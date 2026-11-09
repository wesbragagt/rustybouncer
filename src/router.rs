//! Conservative, simple-query-only routing. Backend identity is always the
//! client's original startup identity; no privileged probe credentials are used.
use super::*;
mod classification;
#[cfg(test)]
mod classifier_reference;
mod cte;
use classification::{allowed_function, classify};
mod wire;
use std::{
    collections::HashMap,
    sync::atomic::{AtomicUsize, Ordering},
};
use tokio::io::BufReader;
use wire::{send, Response};

const MAX_MESSAGE: usize = 16 * 1024 * 1024;
static NEXT_REPLICA: AtomicUsize = AtomicUsize::new(0);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Read,
    Write,
    Transaction,
    Stateful,
    Unsupported,
}

async fn message(stream: &mut TcpStream) -> Result<(u8, Vec<u8>)> {
    let tag = stream.read_u8().await?;
    let size = stream.read_u32().await? as usize;
    if !(4..=MAX_MESSAGE).contains(&size) {
        bail!("invalid or oversized protocol message");
    }
    let mut body = vec![0; size - 4];
    stream.read_exact(&mut body).await?;
    Ok((tag, body))
}

struct Backend {
    stream: BufReader<TcpStream>,
    _live: Guard,
    generation: u64,
}

async fn handshake(
    mut stream: TcpStream,
    packet: &[u8],
    generation: u64,
    live: Guard,
) -> Result<(Backend, Vec<(u8, Vec<u8>)>)> {
    stream.set_nodelay(true)?;
    stream.write_all(packet).await?;
    let mut startup = Vec::new();
    let mut total = 0;
    loop {
        let (tag, body) = message(&mut stream).await?;
        total += body.len();
        if total > MAX_MESSAGE {
            bail!("oversized backend startup");
        }
        if tag == b'R' && body != [0, 0, 0, 0] {
            bail!("routed mode requires trust-authenticated backend connections");
        }
        if tag == b'E' {
            bail!("backend rejected startup");
        }
        let ready = tag == b'Z';
        if tag != b'K' {
            startup.push((tag, body));
        } // cancellation is unsupported
        if ready {
            return Ok((
                Backend {
                    stream: BufReader::new(stream),
                    generation,
                    _live: live,
                },
                startup,
            ));
        }
    }
}

async fn connect(
    config: &Config,
    node: usize,
    packet: &[u8],
    generation: u64,
    ticket: &mut Option<Guard>,
) -> Result<(Backend, Vec<(u8, Vec<u8>)>)> {
    timeout(Duration::from_millis(config.probe_timeout_ms), async {
        let (stream, live) = connect_tcp(config, node, ticket).await?;
        handshake(stream, packet, generation, live).await
    })
    .await
    .context("backend handshake timed out")?
}

async fn result(backend: &mut Backend, query: &[u8]) -> Result<(Response, u8)> {
    send(&mut backend.stream, b'Q', query).await?;
    wire::response(&mut backend.stream).await
}

async fn primary_changed(topology: &mut watch::Receiver<Topology>, generation: u64) -> Result<()> {
    loop {
        if topology.borrow_and_update().generation != generation {
            bail!("session rejected");
        }
        topology.changed().await?;
    }
}

// A promotion changes eligibility for new work, not the health of an already
// dispatched read. The same TCP backend may drain as either replica or primary.
async fn read_backend_unavailable(
    topology: &mut watch::Receiver<Topology>,
    node: usize,
) -> Result<()> {
    loop {
        let current = topology.borrow_and_update().clone();
        if current.ambiguous || (!current.replicas.contains(&node) && current.primary != Some(node))
        {
            bail!("session rejected");
        }
        topology.changed().await?;
    }
}

async fn wait_route(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    read: bool,
    failed: &HashSet<usize>,
) -> Result<(usize, Topology)> {
    let _wait = config.metrics.timer("route_wait_duration_seconds");
    let deadline = Instant::now() + Duration::from_millis(config.wait_timeout_ms);
    loop {
        let current = topology.borrow_and_update().clone();
        if !current.ambiguous {
            let replicas: Vec<_> = current
                .replicas
                .iter()
                .copied()
                .filter(|i| !failed.contains(i))
                .collect();
            if read && !replicas.is_empty() {
                let node = replicas[NEXT_REPLICA.fetch_add(1, Ordering::Relaxed) % replicas.len()];
                return Ok((node, current));
            }
            if let Some(primary) = current.primary.filter(|i| !failed.contains(i)) {
                return Ok((primary, current));
            }
        }
        if Instant::now() >= deadline {
            bail!("no eligible backend before routing deadline");
        }
        tokio::select! {
            changed = topology.changed() => { changed?; }
            _ = sleep(Duration::from_millis(30)) => {}
        }
    }
}

type Messages = Vec<(u8, Vec<u8>)>;
type QueryResult = (Response, u8);

struct SessionState {
    backends: HashMap<usize, Backend>,
    transaction: bool,
    pinned: bool,
    primary_generation: u64,
}

async fn initial_backend(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    packet: &[u8],
    ticket: &mut Option<Guard>,
) -> Result<(usize, Backend, Messages)> {
    loop {
        let (stream, selected, live) = wait_backend(config, topology, ticket).await?;
        let (backend, messages) = timeout(
            Duration::from_millis(config.startup_timeout_ms),
            handshake(stream, packet, selected.generation, live),
        )
        .await??;
        if topology.borrow().generation == selected.generation {
            return Ok((selected.primary.unwrap(), backend, messages));
        }
    }
}

async fn receive_query(
    client: &mut TcpStream,
    topology: &mut watch::Receiver<Topology>,
    state: &SessionState,
) -> Result<(u8, Vec<u8>)> {
    if state.transaction || state.pinned {
        tokio::select! {
            incoming = message(client) => incoming,
            changed = primary_changed(topology, state.primary_generation) => { changed?; bail!("primary changed during pinned session or transaction"); }
        }
    } else {
        message(client).await
    }
}

async fn prepare_backend(
    config: &Config,
    node: usize,
    packet: &[u8],
    selected: &Topology,
    read: bool,
    deadline: Instant,
    ticket: &mut Option<Guard>,
) -> Result<Backend> {
    let (mut backend, _) = connect(config, node, packet, selected.generation, ticket).await?;
    if read && selected.replicas.contains(&node) {
        let (messages, status) = timeout(
            deadline
                .saturating_duration_since(Instant::now())
                .min(Duration::from_millis(config.probe_timeout_ms)),
            result(&mut backend, b"SET default_transaction_read_only = on\0"),
        )
        .await??;
        if status != b'I' || messages.sql_error {
            bail!("replica initialization failed");
        }
    }
    Ok(backend)
}

async fn execute_query(
    backend: &mut Backend,
    body: &[u8],
    topology: &mut watch::Receiver<Topology>,
    selected: &Topology,
    node: usize,
    timing: (bool, Instant),
    metrics: &Metrics,
) -> Result<QueryResult> {
    let (read, deadline) = timing;
    let mut ticket = None;
    let execution = async {
        ticket = Some(
            metrics.guard(Event::Attempt(if selected.replicas.contains(&node) {
                "replica"
            } else {
                "primary"
            })),
        );
        result(backend, body).await
    };
    let response = if read {
        tokio::select! {
            response = timeout(deadline.saturating_duration_since(Instant::now()), execution) => response.map_err(anyhow::Error::from).and_then(|r| r),
            unavailable = read_backend_unavailable(topology, node) => { Err(unavailable.err().unwrap_or_else(|| anyhow::anyhow!("read backend unavailable or topology ambiguous"))) }
        }
    } else {
        tokio::select! {
            response = execution => response,
            changed = primary_changed(topology, selected.generation) => { Err(changed.err().unwrap_or_else(|| anyhow::anyhow!("primary changed after query dispatch; outcome may be unknown"))) }
        }
    };
    let outcome = match &response {
        Ok((messages, _)) if messages.sql_error => Outcome::SqlError,
        Ok(_) => Outcome::Ok,
        Err(_) => Outcome::Error,
    };
    finish_ticket(&mut ticket, outcome);
    response
}

async fn route_query(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    packet: &[u8],
    state: &mut SessionState,
    body: &[u8],
    kind: Kind,
) -> Result<Option<Response>> {
    let read = kind == Kind::Read && !state.transaction && !state.pinned;
    let routing_deadline = Instant::now() + Duration::from_millis(config.wait_timeout_ms);
    // One wall-clock budget spans routing, setup and all buffered read attempts.
    // Each new routing phase has its own wait limit; healthy execution does not.
    let total_deadline = Instant::now() + Duration::from_millis(config.read_retry_timeout_ms);
    let mut attempts = 0;
    let mut failed = HashSet::new();
    loop {
        if read && (attempts >= config.read_max_attempts || Instant::now() >= total_deadline) {
            return Ok(None);
        }
        let deadline = Instant::now() + Duration::from_millis(config.wait_timeout_ms);
        let deadline = if read {
            deadline.min(total_deadline)
        } else {
            routing_deadline
        };
        let (node, selected) = match timeout(
            deadline.saturating_duration_since(Instant::now()),
            wait_route(config, topology, read, &failed),
        )
        .await
        {
            Ok(Ok(value)) => value,
            _ => return Ok(None),
        };
        if (state.transaction || state.pinned) && selected.generation != state.primary_generation {
            bail!("primary changed during pinned session or transaction");
        }
        if state
            .backends
            .get(&node)
            .is_some_and(|b| b.generation != selected.generation)
        {
            state.backends.remove(&node);
        }
        // Count selections, including handshake failures and pre-dispatch races.
        if read && attempts > 0 {
            config.metrics.inc("read_retries_total", &[]);
        }
        attempts += 1;
        if let std::collections::hash_map::Entry::Vacant(entry) = state.backends.entry(node) {
            let mut ticket = None;
            let prepared = timeout(
                deadline.saturating_duration_since(Instant::now()),
                prepare_backend(config, node, packet, &selected, read, deadline, &mut ticket),
            )
            .await;
            finish_ticket(&mut ticket, Outcome::Error);
            match prepared {
                Ok(Ok(backend)) => {
                    entry.insert(backend);
                }
                _ => {
                    failed.insert(node);
                    continue;
                }
            }
        }
        // Revalidate before the first query byte, including split-brain state.
        if *topology.borrow() != selected {
            continue;
        }
        match execute_query(
            state.backends.get_mut(&node).unwrap(),
            body,
            topology,
            &selected,
            node,
            (
                read,
                if read {
                    (Instant::now() + Duration::from_millis(config.query_timeout_ms))
                        .min(total_deadline)
                } else {
                    Instant::now() + Duration::from_millis(config.query_timeout_ms)
                },
            ),
            &config.metrics,
        )
        .await
        {
            Ok((messages, status)) => {
                state.transaction = status != b'I';
                state.pinned |= kind == Kind::Stateful;
                if !read {
                    state.primary_generation = selected.generation;
                }
                return Ok(Some(messages));
            }
            Err(error) if read => {
                warn!(node, %error, "retrying buffered pure read on another eligible backend");
                state.backends.remove(&node);
                failed.insert(node);
            }
            Err(error) => {
                return Err(error
                    .context("query was dispatched; outcome may be unknown and was not replayed"))
            }
        }
    }
}

pub(super) async fn session(
    mut client: TcpStream,
    config: Arc<Config>,
    mut topology: watch::Receiver<Topology>,
) -> Result<()> {
    client.set_nodelay(true)?;
    let packet = timeout(
        Duration::from_millis(config.startup_timeout_ms),
        startup(&mut client),
    )
    .await??;
    let mut ticket = None;
    let initial = timeout(
        Duration::from_millis(config.wait_timeout_ms),
        initial_backend(&config, &mut topology, &packet, &mut ticket),
    )
    .await;
    finish_ticket(&mut ticket, Outcome::Error);
    let (primary, backend, messages) = match initial {
        Ok(Ok(value)) => value,
        _ => {
            pg_error(&mut client, "08006", "rustybouncer: startup unavailable; routed mode requires trust-authenticated backends").await?;
            bail!("session rejected");
        }
    };
    for (tag, body) in messages {
        send(&mut client, tag, &body).await?;
    }
    let mut state = SessionState {
        backends: HashMap::from([(primary, backend)]),
        transaction: false,
        pinned: false,
        primary_generation: topology.borrow().generation,
    };
    loop {
        let (tag, body) = receive_query(&mut client, &mut topology, &state).await?;
        if tag == b'X' {
            return Ok(());
        }
        if tag != b'Q' {
            pg_error(&mut client, "0A000", "rustybouncer routed mode supports simple Query protocol only; disable prepared/extended queries").await?;
            bail!("session rejected");
        }
        handle_query(
            &mut client,
            &config,
            &mut topology,
            &packet,
            &mut state,
            &body,
        )
        .await?;
    }
}
fn query_kind(body: &[u8]) -> Option<Kind> {
    let sql = body
        .strip_suffix(&[0])
        .and_then(|b| std::str::from_utf8(b).ok())?;
    if sql.contains('\0') {
        return None;
    }
    Some(classify(sql))
}
impl Kind {
    fn label(self) -> &'static str {
        match self {
            Self::Read => "read",
            Self::Write => "write",
            Self::Transaction => "transaction",
            Self::Stateful => "stateful",
            Self::Unsupported => "unsupported",
        }
    }
}
async fn handle_query(
    client: &mut TcpStream,
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    packet: &[u8],
    state: &mut SessionState,
    body: &[u8],
) -> Result<()> {
    let mut accounting = config.metrics.guard(Event::Query("malformed"));
    let kind = query_kind(body);
    accounting.classify_query(kind.map(Kind::label).unwrap_or("malformed"));
    deliver_query(
        client,
        config,
        topology,
        packet,
        state,
        (body, kind),
        &mut accounting,
    )
    .await
}
async fn deliver_query(
    client: &mut TcpStream,
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    packet: &[u8],
    state: &mut SessionState,
    query: (&[u8], Option<Kind>),
    accounting: &mut Guard,
) -> Result<()> {
    let (body, kind) = query;
    let Some(kind) = kind else {
        accounting.finish(Outcome::RouterError);
        bail!("malformed Query message");
    };
    if kind == Kind::Unsupported {
        fatal_query(
            client,
            "0A000",
            "COPY is unsupported in routed mode",
            accounting,
        )
        .await?;
        bail!("unsupported query");
    }
    let routed = match route_query(config, topology, packet, state, body, kind).await {
        Ok(value) => value,
        Err(error) => {
            accounting.finish(Outcome::RouterError);
            return Err(error);
        }
    };
    let Some(messages) = routed else {
        fatal_query(
            client,
            "08006",
            "rustybouncer: routing or read retry budget exhausted",
            accounting,
        )
        .await?;
        bail!("routing budget exhausted");
    };
    let sql_error = messages.sql_error;
    if let Err(error) = client.write_all(&messages.bytes).await {
        accounting.finish(Outcome::DeliveryError);
        return Err(error.into());
    }
    accounting.finish(if sql_error {
        Outcome::SqlError
    } else {
        Outcome::Ok
    });
    Ok(())
}
async fn fatal_query(
    client: &mut TcpStream,
    code: &str,
    message: &str,
    accounting: &mut Guard,
) -> Result<()> {
    let result = pg_error(client, code, message).await;
    accounting.finish(if result.is_ok() {
        Outcome::RouterError
    } else {
        Outcome::DeliveryError
    });
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    fn measured_config() -> Config {
        let mut c: Config = toml::from_str(include_str!("../rustybouncer.toml")).unwrap();
        c.metrics = Metrics::new(true, &c.nodes, true, c.max_clients).unwrap();
        c
    }
    fn text(m: &Metrics) -> String {
        String::from_utf8(m.render().unwrap()).unwrap()
    }
    #[test]
    fn malformed_query_body_labels_are_finite() {
        for body in [
            &b"SELECT 1"[..],
            &b"SELECT\0hidden\0"[..],
            &[255, 0][..],
            &b""[..],
        ] {
            assert!(query_kind(body).is_none());
        }
        assert_eq!(query_kind(b"SELECT 1\0"), Some(Kind::Read));
        assert_eq!(query_kind(b"COPY x TO STDOUT\0"), Some(Kind::Unsupported));
    }
    #[tokio::test]
    async fn framing_boundary_excludes_truncated_and_oversized_queries() {
        for bytes in [
            &[b'Q', 0, 0, 0, 3][..],
            &[b'Q', 1, 0, 0, 1][..],
            &[b'Q', 0, 0, 0, 9, b'x'][..],
        ] {
            let (mut client, mut server) = crate::tests::socket_pair().await;
            client.write_all(bytes).await.unwrap();
            client.shutdown().await.unwrap();
            assert!(message(&mut server).await.is_err());
        }
        let c = measured_config();
        let (mut client, mut server) = crate::tests::socket_pair().await;
        send(&mut client, b'Q', &[255, 0]).await.unwrap();
        let (tag, body) = message(&mut server).await.unwrap();
        assert_eq!(tag, b'Q');
        let (_tx, mut rx) = watch::channel(Topology::default());
        let mut state = SessionState {
            backends: HashMap::new(),
            transaction: false,
            pinned: false,
            primary_generation: 0,
        };
        assert!(
            handle_query(&mut server, &c, &mut rx, &[], &mut state, &body)
                .await
                .is_err()
        );
        assert!(text(&c.metrics)
            .contains("rustybouncer_queries_total{kind=\"malformed\",outcome=\"router_error\"} 1"));
    }
    #[tokio::test]
    async fn dispatch_sql_error_and_deadline_are_distinct() {
        for sql_error in [true, false] {
            let c = measured_config();
            let (stream, mut server) = crate::tests::socket_pair().await;
            let mut backend = Backend {
                stream: BufReader::new(stream),
                generation: 0,
                _live: c.metrics.guard(Event::Backend("node-a".into())),
            };
            let topology = Topology {
                primary: Some(0),
                ..Topology::default()
            };
            let (_tx, mut rx) = watch::channel(topology.clone());
            let task = tokio::spawn(async move {
                assert_eq!(message(&mut server).await.unwrap().0, b'Q');
                if sql_error {
                    send(&mut server, b'E', b"error").await.unwrap();
                    send(&mut server, b'Z', b"I").await.unwrap();
                } else {
                    std::future::pending::<()>().await;
                }
            });
            let response = execute_query(
                &mut backend,
                b"SELECT 1\0",
                &mut rx,
                &topology,
                0,
                (true, Instant::now() + Duration::from_millis(20)),
                &c.metrics,
            )
            .await;
            assert_eq!(response.is_ok(), sql_error);
            let outcome = if sql_error { "sql_error" } else { "error" };
            assert!(text(&c.metrics).contains(&format!(
                "rustybouncer_query_attempts_total{{outcome=\"{outcome}\",route=\"primary\"}} 1"
            )));
            task.abort();
        }
    }
    #[tokio::test]
    async fn topology_dropped_dispatch_is_error_external_abort_is_aborted() {
        for abort in [true, false] {
            let c = measured_config();
            let m = c.metrics.clone();
            let (stream, mut server) = crate::tests::socket_pair().await;
            let backend = Backend {
                stream: BufReader::new(stream),
                generation: 0,
                _live: m.guard(Event::Backend("node-a".into())),
            };
            let selected = Topology {
                primary: Some(0),
                ..Topology::default()
            };
            let (tx, mut rx) = watch::channel(selected.clone());
            let task = tokio::spawn(async move {
                let mut backend = backend;
                execute_query(
                    &mut backend,
                    b"SELECT 1\0",
                    &mut rx,
                    &selected,
                    0,
                    (true, Instant::now() + Duration::from_secs(60)),
                    &m,
                )
                .await
            });
            assert_eq!(message(&mut server).await.unwrap().0, b'Q');
            if abort {
                task.abort();
                assert!(task.await.unwrap_err().is_cancelled());
            } else {
                tx.send_replace(Topology::default());
                assert!(task.await.unwrap().is_err());
            }
            let outcome = if abort { "aborted" } else { "error" };
            assert!(text(&c.metrics).contains(&format!(
                "rustybouncer_query_attempts_total{{outcome=\"{outcome}\",route=\"primary\"}} 1"
            )));
        }
    }
    #[test]
    fn read_only_ctes_route_to_replicas() {
        for sql in [
            "WITH x AS (SELECT 1 AS id) SELECT id FROM x",
            "with x(id) as (select 1), y as (select id from x) select * from y;",
            "WITH x AS MATERIALIZED (SELECT 1) SELECT * FROM x",
            "WITH x AS NOT MATERIALIZED (SELECT 1) SELECT * FROM x",
            "WITH RECURSIVE x(n) AS (VALUES (1) UNION ALL SELECT n + 1 FROM x WHERE n < 3) SELECT sum(n) FROM x",
            "WITH x AS (WITH y AS (SELECT 1) SELECT * FROM y) SELECT * FROM x",
            "WITH x AS (SELECT 'INSERT UPDATE DELETE FOR' AS text) SELECT text FROM x",
            "WITH x AS (SELECT pg_catalog.current_setting('cluster_name') AS node) SELECT node FROM x",
        ] {
            assert_eq!(classify(sql), Kind::Read, "{sql}");
        }
    }

    #[test]
    fn unsafe_and_unrecognized_ctes_stay_on_primary() {
        for sql in [
            "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
            "WITH x AS (INSERT INTO t VALUES (1)) SELECT 1",
            "WITH x AS (UPDATE t SET id = 1 RETURNING *) SELECT * FROM x",
            "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
            "WITH x AS (SELECT 1) UPDATE t SET id = 2",
            "WITH x AS (SELECT 1) DELETE FROM t",
            "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
            "WITH x AS (SELECT * FROM t FOR SHARE) SELECT * FROM x",
            "WITH x AS (SELECT * FROM t) SELECT * INTO new_table FROM x",
            "WITH x AS (SELECT nextval('seq')) SELECT * FROM x",
            "WITH x AS (SELECT set_config('search_path', 'public', false)) SELECT * FROM x",
            "WITH x AS (SELECT * FROM custom_func()) SELECT * FROM x",
            "WITH x(n) AS (SELECT 1) SELECT x()",
            "WITH x AS (WITH y AS (SELECT custom_func()) SELECT * FROM y) SELECT * FROM x",
            "WITH x AS (SELECT 1) SELECT evil.count(*) FROM x",
            "WITH x AS (SELECT 1) SELECT 1; SELECT 2",
            "WITH x AS (SELECT 1) SELECT",
            "WITH x AS (SELECT /* hidden */ 1) SELECT * FROM x",
        ] {
            assert_eq!(classify(sql), Kind::Stateful, "{sql}");
        }
    }
    #[test]
    fn pure_read_allowlist() {
        for sql in [
            "SELECT 1",
            "select current_setting('cluster_name')",
            "SELECT id FROM items WHERE id = 2",
            "SELECT count(*) FROM items;",
            "SELECT pg_is_in_recovery(), inet_server_port()",
        ] {
            assert_eq!(classify(sql), Kind::Read, "{sql}");
        }
    }
    #[test]
    fn unsafe_selects_are_pinned() {
        for sql in [
            "SELECT nextval('seq')",
            "SELECT evil.current_setting('cluster_name')",
            "SELECT * INTO temp FROM items",
            "SELECT * FROM items FOR UPDATE",
            "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
            "SELECT set_config('search_path', 'x', false)",
            "SELECT custom_func()",
            "SELECT 1; DELETE FROM items",
            "SELECT /*hidden*/ 1",
            "SELECT \"nextval\"('seq')",
            "SELECT 'a\\'",
            "SELECT 1 OPERATOR(public.===) 2",
        ] {
            assert_eq!(classify(sql), Kind::Stateful, "{sql}");
        }
    }
    #[test]
    fn writes_and_transactions_stay_primary() {
        assert_eq!(classify("INSERT INTO items VALUES (1)"), Kind::Write);
        assert_eq!(classify("UPDATE items SET id = 2"), Kind::Write);
        assert_eq!(classify("BEGIN"), Kind::Transaction);
        assert_eq!(classify("SET search_path = public"), Kind::Stateful);
        assert_eq!(classify("COPY items TO STDOUT"), Kind::Unsupported);
    }
}
