//! Conservative, simple-query-only routing. Backend identity is always the
//! client's original startup identity; no privileged probe credentials are used.
use super::*;
mod cte;
use std::{
    collections::HashMap,
    sync::atomic::{AtomicUsize, Ordering},
};

const MAX_MESSAGE: usize = 16 * 1024 * 1024;
const MAX_RESULT: usize = 32 * 1024 * 1024;
static NEXT_REPLICA: AtomicUsize = AtomicUsize::new(0);

#[derive(Debug, PartialEq, Eq)]
enum Kind {
    Read,
    Write,
    Transaction,
    Stateful,
    Unsupported,
}

// Deliberately smaller than SQL's SELECT grammar. Unknown functions, quoted
// identifiers, dollar quotes and comments go to the primary and pin the session.
// A prefix test is insufficient: SELECT nextval(), SELECT INTO and locking
// SELECTs are not pure reads. Replica backends also enforce read-only mode.
fn allowed_function(name: &str) -> bool {
    matches!(
        name,
        "COUNT"
            | "SUM"
            | "AVG"
            | "MIN"
            | "MAX"
            | "COALESCE"
            | "NULLIF"
            | "LOWER"
            | "UPPER"
            | "LENGTH"
            | "ABS"
            | "ROUND"
            | "CURRENT_SETTING"
            | "PG_IS_IN_RECOVERY"
            | "INET_SERVER_ADDR"
            | "INET_SERVER_PORT"
    )
}

fn string_end(bytes: &[u8], mut i: usize) -> Option<usize> {
    i += 1;
    loop {
        match *bytes.get(i)? {
            b'\\' => return None,
            b'\'' => {
                i += 1;
                if bytes.get(i) != Some(&b'\'') {
                    return Some(i);
                }
            }
            _ => {}
        }
        i += 1;
    }
}

fn function_token_allowed(
    sql: &str,
    start: usize,
    end: usize,
    word: &str,
    previous: Option<&str>,
    is_cte: bool,
) -> bool {
    if !sql[end..].trim_start().starts_with('(') {
        return true;
    }
    if sql[..start].trim_end().ends_with('.') && previous != Some("PG_CATALOG") {
        return false;
    }
    is_cte
        || allowed_function(word)
        || matches!(word, "IN" | "EXISTS" | "ANY" | "ALL" | "AS" | "VALUES")
}

fn scan_words(sql: &str, is_cte: bool) -> Option<Vec<String>> {
    let bytes = sql.as_bytes();
    let mut words = Vec::<String>::new();
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'\'' => i = string_end(bytes, i)?,
            b'"' | b'$' => return None,
            b'-' if bytes.get(i + 1) == Some(&b'-') => return None,
            b'/' if bytes.get(i + 1) == Some(&b'*') => return None,
            b';' => {
                if !sql[i + 1..].trim().is_empty() {
                    return None;
                }
                i += 1;
            }
            c if c.is_ascii_alphabetic() || c == b'_' => {
                let start = i;
                while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
                    i += 1;
                }
                let word = sql[start..i].to_ascii_uppercase();
                if !function_token_allowed(
                    sql,
                    start,
                    i,
                    &word,
                    words.last().map(String::as_str),
                    is_cte,
                ) {
                    return None;
                }
                words.push(word);
            }
            c if !c.is_ascii() => return None,
            _ => i += 1,
        }
    }
    Some(words)
}

fn classify(sql: &str) -> Kind {
    let is_cte = sql
        .trim_start()
        .split(|c: char| !c.is_ascii_alphanumeric() && c != '_')
        .next()
        .is_some_and(|word| word.eq_ignore_ascii_case("WITH"));
    if is_cte && !cte::read_only(sql) {
        return Kind::Stateful;
    }
    let Some(words) = scan_words(sql, is_cte) else {
        return Kind::Stateful;
    };
    let first = words.first().map(String::as_str).unwrap_or("");
    if first == "COPY" {
        return Kind::Unsupported;
    }
    if (first == "WITH" || first == "SELECT")
        && words
            .iter()
            .any(|w| matches!(w.as_str(), "INTO" | "FOR" | "LOCK" | "OPERATOR"))
    {
        return Kind::Stateful;
    }
    match first {
        "SELECT" | "WITH"
            if !words.iter().any(|w| {
                matches!(
                    w.as_str(),
                    "INSERT" | "UPDATE" | "DELETE" | "MERGE" | "CREATE" | "DROP" | "ALTER" | "SET"
                )
            }) =>
        {
            Kind::Read
        }
        "INSERT" | "UPDATE" | "DELETE" | "MERGE" => Kind::Write,
        "BEGIN" | "START" | "COMMIT" | "END" | "ROLLBACK" | "ABORT" | "SAVEPOINT" | "RELEASE" => {
            Kind::Transaction
        }
        _ => Kind::Stateful,
    }
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

async fn send(stream: &mut TcpStream, tag: u8, body: &[u8]) -> Result<()> {
    stream.write_u8(tag).await?;
    stream.write_u32((body.len() + 4) as u32).await?;
    stream.write_all(body).await?;
    Ok(())
}

struct Backend {
    stream: TcpStream,
    generation: u64,
}

async fn handshake(
    mut stream: TcpStream,
    packet: &[u8],
    generation: u64,
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
            return Ok((Backend { stream, generation }, startup));
        }
    }
}

async fn connect(
    config: &Config,
    node: usize,
    packet: &[u8],
    generation: u64,
) -> Result<(Backend, Vec<(u8, Vec<u8>)>)> {
    timeout(Duration::from_millis(config.probe_timeout_ms), async {
        let node = &config.nodes[node];
        let stream = TcpStream::connect((node.host.as_str(), node.port)).await?;
        handshake(stream, packet, generation).await
    })
    .await
    .context("backend handshake timed out")?
}

async fn result(backend: &mut Backend, query: &[u8]) -> Result<(Vec<(u8, Vec<u8>)>, u8)> {
    send(&mut backend.stream, b'Q', query).await?;
    let mut messages = Vec::new();
    let mut total = 0;
    loop {
        let (tag, body) = message(&mut backend.stream).await?;
        total += body.len() + 5;
        if total > MAX_RESULT {
            bail!("routed result exceeds 32 MiB limit");
        }
        if matches!(tag, b'G' | b'H' | b'W') {
            bail!("COPY is unsupported in routed mode");
        }
        if tag == b'Z' {
            let status = *body.first().context("missing transaction status")?;
            messages.push((tag, body));
            return Ok((messages, status));
        }
        messages.push((tag, body));
    }
}

async fn primary_changed(topology: &mut watch::Receiver<Topology>, generation: u64) -> Result<()> {
    loop {
        if topology.borrow_and_update().generation != generation {
            return Ok(());
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
            return Ok(());
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
type QueryResult = (Messages, u8);

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
) -> Result<(usize, Backend, Messages)> {
    loop {
        let (stream, selected) = wait_backend(config, topology).await?;
        let (backend, messages) = timeout(
            Duration::from_millis(config.startup_timeout_ms),
            handshake(stream, packet, selected.generation),
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
) -> Result<Backend> {
    let (mut backend, _) = connect(config, node, packet, selected.generation).await?;
    if read && selected.replicas.contains(&node) {
        let (messages, status) = timeout(
            deadline
                .saturating_duration_since(Instant::now())
                .min(Duration::from_millis(config.probe_timeout_ms)),
            result(&mut backend, b"SET default_transaction_read_only = on\0"),
        )
        .await??;
        if status != b'I' || messages.iter().any(|(tag, _)| *tag == b'E') {
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
    read: bool,
    deadline: Instant,
) -> Result<QueryResult> {
    let execution = result(backend, body);
    if read {
        tokio::select! {
            response = timeout(deadline.saturating_duration_since(Instant::now()), execution) => response.map_err(anyhow::Error::from).and_then(|r| r),
            unavailable = read_backend_unavailable(topology, node) => { unavailable?; Err(anyhow::anyhow!("read backend unavailable or topology ambiguous")) }
        }
    } else {
        tokio::select! {
            response = execution => response,
            changed = primary_changed(topology, selected.generation) => { changed?; Err(anyhow::anyhow!("primary changed after query dispatch; outcome may be unknown")) }
        }
    }
}

async fn route_query(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    packet: &[u8],
    state: &mut SessionState,
    body: &[u8],
    kind: Kind,
) -> Result<Option<Messages>> {
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
        attempts += 1;
        if let std::collections::hash_map::Entry::Vacant(entry) = state.backends.entry(node) {
            match timeout(
                deadline.saturating_duration_since(Instant::now()),
                prepare_backend(config, node, packet, &selected, read, deadline),
            )
            .await
            {
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
            read,
            if read {
                (Instant::now() + Duration::from_millis(config.query_timeout_ms))
                    .min(total_deadline)
            } else {
                Instant::now() + Duration::from_millis(config.query_timeout_ms)
            },
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
    let (primary, backend, messages) = match timeout(
        Duration::from_millis(config.wait_timeout_ms),
        initial_backend(&config, &mut topology, &packet),
    )
    .await
    {
        Ok(Ok(value)) => value,
        _ => {
            pg_error(&mut client, "08006", "rustybouncer: startup unavailable; routed mode requires trust-authenticated backends").await?;
            return Ok(());
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
            return Ok(());
        }
        let sql = match body
            .strip_suffix(&[0])
            .and_then(|bytes| std::str::from_utf8(bytes).ok())
        {
            Some(sql) if !sql.contains('\0') => sql,
            _ => bail!("malformed Query message"),
        };
        let kind = classify(sql);
        if kind == Kind::Unsupported {
            pg_error(&mut client, "0A000", "COPY is unsupported in routed mode").await?;
            return Ok(());
        }
        let Some(messages) =
            route_query(&config, &mut topology, &packet, &mut state, &body, kind).await?
        else {
            pg_error(
                &mut client,
                "08006",
                "rustybouncer: routing or read retry budget exhausted",
            )
            .await?;
            return Ok(());
        };
        for (tag, body) in messages {
            send(&mut client, tag, &body).await?;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
