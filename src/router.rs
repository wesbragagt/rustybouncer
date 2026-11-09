//! Conservative, simple-query-only routing. Backend identity is always the
//! client's original startup identity; no privileged probe credentials are used.
use super::*;
mod affinity;
mod classification;
#[cfg(test)]
mod classifier_reference;
mod cte;
mod frontend;
pub(crate) mod pool;
use classification::{allowed_function, classify};
mod wire;
use std::{
    collections::HashMap,
    sync::atomic::{AtomicUsize, Ordering},
};
use tokio::{
    io::{AsyncRead, BufReader},
    net::tcp::OwnedWriteHalf,
    sync::mpsc,
};
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

async fn message<R: AsyncRead + Unpin>(stream: &mut R) -> Result<(u8, Vec<u8>)> {
    let tag = stream.read_u8().await?;
    message_body(stream, tag).await
}
async fn message_body<R: AsyncRead + Unpin>(stream: &mut R, tag: u8) -> Result<(u8, Vec<u8>)> {
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
    buffers: buffers::Pool,
    cancel_identity: (u32, u32),
    status: u8,
    startup_metadata: Option<Arc<Messages>>,
    idle_since: Instant,
    slot: Option<pool::Slot>,
    pool_owner: Option<pool::Manager>,
}
impl Backend {
    fn healthy_idle(&self) -> bool {
        if !self.stream.buffer().is_empty() {
            return false;
        }
        matches!(self.stream.get_ref().try_read(&mut [0u8;1]),Err(error) if error.kind()==std::io::ErrorKind::WouldBlock)
    }
}

async fn handshake(
    mut stream: TcpStream,
    packet: &[u8],
    generation: u64,
    live: Guard,
    buffers: buffers::Pool,
) -> Result<(Backend, Vec<(u8, Vec<u8>)>)> {
    stream.set_nodelay(true)?;
    stream.write_all(packet).await?;
    let mut startup = Vec::new();
    let mut cancel_identity = None;
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
        if ready && body != *b"I" {
            bail!("invalid backend startup transaction status");
        }
        if tag == b'K' {
            if body.len() != 8 || cancel_identity.is_some() {
                bail!("invalid backend cancellation identity");
            }
            cancel_identity = Some((
                u32::from_be_bytes(body[..4].try_into().unwrap()),
                u32::from_be_bytes(body[4..].try_into().unwrap()),
            ));
        } else {
            startup.push((tag, body));
        }
        if ready {
            return Ok((
                Backend {
                    stream: BufReader::new(stream),
                    generation,
                    buffers,
                    status: b'I',
                    startup_metadata: None,
                    idle_since: Instant::now(),
                    slot: None,
                    pool_owner: None,
                    cancel_identity: cancel_identity
                        .context("missing backend cancellation identity")?,
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
        handshake(
            stream,
            packet,
            generation,
            live,
            config.result_buffers.clone(),
        )
        .await
    })
    .await
    .context("backend handshake timed out")?
}

async fn result(backend: &mut Backend, query: &[u8]) -> Result<(Response, u8)> {
    send(&mut backend.stream, b'Q', query).await?;
    let response = wire::response(&mut backend.stream, &backend.buffers).await?;
    backend.status = response.1;
    Ok(response)
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
    identity: Arc<[u8]>,
    transaction: bool,
    status: u8,
    pinned: bool,
    primary_generation: u64,
}

impl SessionState {
    fn read_eligible(&self, kind: Kind, mode: pool::Mode) -> bool {
        kind == Kind::Read && !self.transaction && !self.pinned && mode != pool::Mode::Session
    }
    fn discard_unassigned(&mut self, mode: pool::Mode) {
        if mode != pool::Mode::None && !self.transaction && !self.pinned && self.status == b'I' {
            self.backends.clear();
        }
    }
    fn record_result(
        &mut self,
        config: &Config,
        body: &[u8],
        kind: Kind,
        read: bool,
        generation: u64,
        status: u8,
    ) -> Result<()> {
        self.transaction = status != b'I';
        self.status = status;
        self.pinned |= if config.pool_mode == pool::Mode::Transaction {
            let sql = std::str::from_utf8(
                body.strip_suffix(b"\0")
                    .context("invalid query terminator")?,
            )?;
            affinity::session_required(sql, kind)
        } else {
            kind == Kind::Stateful
        };
        if !read {
            self.primary_generation = generation;
        }
        Ok(())
    }
    fn validate_backend(&mut self, node: usize, selected: &Topology) -> Result<()> {
        if (self.transaction || self.pinned) && selected.generation != self.primary_generation {
            bail!("primary changed during pinned session or transaction");
        }
        if self
            .backends
            .get(&node)
            .is_some_and(|b| b.generation != selected.generation)
        {
            self.backends.remove(&node);
        }
        Ok(())
    }
}

async fn initial_backend(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    packet: &[u8],
    ticket: &mut Option<Guard>,
) -> Result<(usize, Option<Backend>, Arc<Messages>)> {
    if config.pool_mode != pool::Mode::None {
        return pooled_startup(config, topology, packet, ticket).await;
    }
    loop {
        let (stream, selected, live) = wait_backend(config, topology, ticket).await?;
        let (backend, messages) = timeout(
            Duration::from_millis(config.startup_timeout_ms),
            handshake(
                stream,
                packet,
                selected.generation,
                live,
                config.result_buffers.clone(),
            ),
        )
        .await??;
        if topology.borrow().generation == selected.generation {
            return Ok((selected.primary.unwrap(), Some(backend), Arc::new(messages)));
        }
    }
}

async fn receive_query(
    client: &mut mpsc::Receiver<frontend::Incoming>,
    topology: &mut watch::Receiver<Topology>,
    state: &SessionState,
) -> Result<Option<frontend::Incoming>> {
    if state.transaction || state.pinned {
        tokio::select! {
            incoming = client.recv() => Ok(incoming),
            changed = primary_changed(topology, state.primary_generation) => { changed?; bail!("primary changed during pinned session or transaction"); }
        }
    } else {
        Ok(client.recv().await)
    }
}

async fn prepare_backend(
    config: &Config,
    node: usize,
    packet: &Arc<[u8]>,
    selected: &Topology,
    read: bool,
    deadline: Instant,
    ticket: &mut Option<Guard>,
) -> Result<Backend> {
    let mut backend = if config.pool_mode == pool::Mode::None {
        connect(config, node, packet, selected.generation, ticket)
            .await?
            .0
    } else {
        pooled_backend(config, node, packet.clone(), selected, deadline, ticket).await?
    };
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
    context: (&Config, &cancel::Operation),
) -> Result<QueryResult> {
    let (read, deadline) = timing;
    let (config, operation) = context;
    let node_config = &config.nodes[node];
    operation.publish(Arc::new(cancel::Target {
        host: Arc::from(node_config.host.as_str()),
        port: node_config.port,
        pid: backend.cancel_identity.0,
        secret: backend.cancel_identity.1,
    }))?;
    let mut ticket = None;
    let response = {
        let execution = async {
            ticket = Some(config.metrics.guard(Event::Attempt(
                if selected.replicas.contains(&node) {
                    "replica"
                } else {
                    "primary"
                },
            )));
            result(backend, body).await
        };
        tokio::pin!(execution);
        let response = if read {
            tokio::select! {
                response = timeout(deadline.saturating_duration_since(Instant::now()), &mut execution) => response.map_err(anyhow::Error::from).and_then(|r| r),
                _reason = operation.cancelled() => {
                    timeout(Duration::from_millis(config.cancel_cleanup_timeout_ms), &mut execution).await.context("cancelled read did not drain").and_then(|r|r)
                }
                unavailable = read_backend_unavailable(topology, node) => { Err(unavailable.err().unwrap_or_else(|| anyhow::anyhow!("read backend unavailable or topology ambiguous"))) }
            }
        } else {
            tokio::select! {
                response = &mut execution => response,
                _reason = operation.cancelled() => {
                    timeout(Duration::from_millis(config.cancel_cleanup_timeout_ms), &mut execution).await.context("cancelled query did not drain").and_then(|r|r)
                }
                changed = primary_changed(topology, selected.generation) => { Err(changed.err().unwrap_or_else(|| anyhow::anyhow!("primary changed after query dispatch; outcome may be unknown"))) }
            }
        };
        response
    };
    let outcome = match &response {
        Ok((messages, _)) if messages.sql_error => Outcome::SqlError,
        Ok(_) => Outcome::Ok,
        Err(_) => Outcome::Error,
    };
    finish_ticket(&mut ticket, outcome);
    let cancelled = operation
        .retire(Instant::now() + Duration::from_millis(config.cancel_cleanup_timeout_ms))
        .await?;
    if cancelled && response.is_ok() {
        // Awaited CancelRequest EOF precedes this private barrier. It cannot
        // cancel a subsequent user command or a lease handed to another client.
        let (_, barrier_status) = timeout(
            Duration::from_millis(config.cancel_cleanup_timeout_ms),
            result(backend, b"\0"),
        )
        .await??;
        if response
            .as_ref()
            .is_ok_and(|(_, status)| *status != barrier_status)
        {
            bail!("transaction state changed during cancellation cleanup");
        }
    }
    response
}

fn execution_deadline(config: &Config, read: bool, total: Instant) -> Instant {
    let deadline = Instant::now() + Duration::from_millis(config.query_timeout_ms);
    if read {
        deadline.min(total)
    } else {
        deadline
    }
}

enum Setup {
    Ready,
    Stale,
    Failed,
}
async fn ensure_backend(
    config: &Config,
    state: &mut SessionState,
    selected: (usize, &Topology),
    timing: (bool, Instant),
    operation: &cancel::Operation,
) -> Result<Setup> {
    let (node, selected) = selected;
    let (read, deadline) = timing;
    if state.backends.contains_key(&node) {
        return Ok(Setup::Ready);
    }
    let mut ticket = None;
    let prepared = tokio::select! {
        prepared=timeout(deadline.saturating_duration_since(Instant::now()),prepare_backend(config,node,&state.identity,selected,read,deadline,&mut ticket))=>prepared,
        reason=operation.cancelled()=>{
            finish_ticket(&mut ticket,Outcome::Error);
            return Err(cancel::Interrupted(reason).into());
        },
    };
    finish_ticket(&mut ticket, Outcome::Error);
    match prepared {
        Ok(Ok(backend)) => {
            state.backends.insert(node, backend);
            Ok(Setup::Ready)
        }
        Ok(Err(error)) if error.is::<pool::Stale>() => Ok(Setup::Stale),
        Ok(Err(error)) if error.is::<pool::Rejected>() => Err(error),
        Err(_) if config.pool_mode != pool::Mode::None => {
            Err(pool::Rejected("backend lease/setup deadline exhausted").into())
        }
        _ => Ok(Setup::Failed),
    }
}

async fn route_query(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    _packet: &[u8],
    state: &mut SessionState,
    body: &[u8],
    kind: Kind,
    operation: &cancel::Operation,
) -> Result<Option<Response>> {
    let read = state.read_eligible(kind, config.pool_mode);
    let routing_deadline = Instant::now() + Duration::from_millis(config.wait_timeout_ms);
    // One wall-clock budget spans routing, setup and all buffered read attempts.
    // Each new routing phase has its own wait limit; healthy execution does not.
    let total_deadline = Instant::now() + Duration::from_millis(config.read_retry_timeout_ms);
    let mut attempts = 0;
    let mut failed = HashSet::new();
    loop {
        operation.check()?;
        if read && (attempts >= config.read_max_attempts || Instant::now() >= total_deadline) {
            return Ok(None);
        }
        if !read && Instant::now() >= routing_deadline {
            return Ok(None);
        }
        let deadline = Instant::now() + Duration::from_millis(config.wait_timeout_ms);
        let deadline = if read {
            deadline.min(total_deadline)
        } else {
            routing_deadline
        };
        let routed = tokio::select! {
            routed=timeout(deadline.saturating_duration_since(Instant::now()),wait_route(config,topology,read,&failed))=>routed,
            reason=operation.cancelled()=>return Err(cancel::Interrupted(reason).into()),
        };
        let (node, selected) = match routed {
            Ok(Ok(value)) => value,
            _ => return Ok(None),
        };
        state.validate_backend(node, &selected)?;
        // Count selections, including handshake failures and pre-dispatch races.
        if read && attempts > 0 {
            config.metrics.inc("read_retries_total", &[]);
        }
        attempts += 1;
        match ensure_backend(
            config,
            state,
            (node, &selected),
            (read, deadline),
            operation,
        )
        .await?
        {
            Setup::Ready => {}
            Setup::Stale => continue,
            Setup::Failed => {
                failed.insert(node);
                continue;
            }
        }
        // A ready inner future must not bypass an already elapsed outer timeout.
        if Instant::now() >= deadline {
            return Ok(None);
        }
        // Revalidate before the first query byte, including split-brain state.
        if *topology.borrow() != selected {
            state.discard_unassigned(config.pool_mode);
            tokio::task::yield_now().await;
            continue;
        }
        if config.pool_mode == pool::Mode::Session {
            state.pinned = true;
        }
        match execute_query(
            state.backends.get_mut(&node).unwrap(),
            body,
            topology,
            &selected,
            node,
            (read, execution_deadline(config, read, total_deadline)),
            (config, operation),
        )
        .await
        {
            Ok((messages, status)) => {
                state.record_result(config, body, kind, read, selected.generation, status)?;
                return Ok(Some(messages));
            }
            Err(error) if operation.interrupted().is_some() => {
                return Err(error);
            }
            Err(error) if error.is::<buffers::Exhausted>() => {
                state.backends.remove(&node);
                return Err(error);
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
    client: TcpStream,
    config: Arc<Config>,
    mut topology: watch::Receiver<Topology>,
    packet: Vec<u8>,
) -> Result<()> {
    let packet: Arc<[u8]> = packet.into();
    client.set_nodelay(true)?;
    let registration = config.cancellations.register()?;
    let (reader, mut client) = client.into_split();
    let (sender, mut incoming) = mpsc::channel(8);
    let ready = std::sync::atomic::AtomicBool::new(false);
    let result = {
        let reading = frontend::read(reader, sender, registration.control.clone(), config.clone());
        let work = session_work(
            &mut client,
            &config,
            &mut topology,
            &packet,
            &mut incoming,
            (&registration, &ready),
        );
        tokio::pin!(reading, work);
        tokio::select! {
            result=&mut work=>result,
            end=&mut reading=>{
                if !ready.load(Ordering::Relaxed) {
                    // Dropping the owned setup future removes its queue ticket and
                    // connecting reservation immediately, even with unread Q frames.
                    match end {
                        Ok(true)=>Ok(()),
                        Ok(false)=>Err(anyhow::anyhow!("frontend disconnected during startup")),
                        Err(error)=>Err(error),
                    }
                } else {
                    let cleanup=timeout(Duration::from_millis(config.cancel_cleanup_timeout_ms),&mut work).await;
                    match end {
                        Ok(true)=>cleanup.context("frontend termination cleanup timed out")?,
                        Ok(false)=>cleanup.context("frontend disconnect cleanup timed out")?,
                        Err(error)=>Err(error),
                    }
                }
            }
        }
    };
    while let Ok(mut frame) = incoming.try_recv() {
        frame.reject();
    }
    result
}

async fn session_work(
    client: &mut OwnedWriteHalf,
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    packet: &Arc<[u8]>,
    incoming: &mut mpsc::Receiver<frontend::Incoming>,
    context: (&cancel::Registration, &std::sync::atomic::AtomicBool),
) -> Result<()> {
    let (registration, ready) = context;
    let mut ticket = None;
    let initial = timeout(
        Duration::from_millis(config.wait_timeout_ms),
        initial_backend(config, topology, packet, &mut ticket),
    )
    .await;
    finish_ticket(&mut ticket, Outcome::Error);
    let (primary, backend, messages) = match initial {
        Ok(Ok(value)) => value,
        _ => {
            pg_error(client, "08006", "rustybouncer: startup unavailable; routed mode requires trust-authenticated backends").await?;
            bail!("session rejected");
        }
    };
    let (pid, secret) = registration.identity();
    timeout(Duration::from_millis(config.startup_timeout_ms), async {
        for (tag, body) in messages.iter() {
            if *tag == b'Z' {
                let mut identity = [0u8; 8];
                identity[..4].copy_from_slice(&pid.to_be_bytes());
                identity[4..].copy_from_slice(&secret.to_be_bytes());
                send(client, b'K', &identity).await?;
            }
            send(client, *tag, body).await?;
        }
        Ok::<_, anyhow::Error>(())
    })
    .await
    .context("frontend startup delivery timed out")??;
    ready.store(true, Ordering::Relaxed);
    drop(messages);
    let mut state = SessionState {
        backends: backend
            .map(|backend| HashMap::from([(primary, backend)]))
            .unwrap_or_default(),
        identity: packet.clone(),
        transaction: false,
        status: b'I',
        pinned: false,
        primary_generation: topology.borrow().generation,
    };
    let result = async {
        loop {
            let Some(mut frame) = receive_query(incoming, topology, &state).await? else { return Ok::<_, anyhow::Error>(()); };
            if frame.tag == b'X' { return Ok(()); }
            if frame.tag != b'Q' {
                pg_error(client, "0A000", "rustybouncer routed mode supports simple Query protocol only; disable prepared/extended queries").await?;
                bail!("session rejected");
            }
            handle_query(client, config, topology, packet, &mut state,
                (&frame.body, frame.kind, frame.accounting.take().context("missing query accounting")?),
                &registration.control).await?;
        }
    }.await;
    if config.pool_mode != pool::Mode::None && result.is_ok() {
        for backend in std::mem::take(&mut state.backends).into_values() {
            release_backend(config, backend, topology).await;
        }
    }
    result
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
    client: &mut OwnedWriteHalf,
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    packet: &[u8],
    state: &mut SessionState,
    query: (&[u8], Option<Kind>, Guard),
    control: &Arc<cancel::Control>,
) -> Result<()> {
    let (body, kind, mut accounting) = query;
    let operation = match control.begin() {
        Ok(operation) => operation,
        Err(error) => {
            accounting.finish(Outcome::RouterError);
            return Err(error);
        }
    };
    let result = deliver_query(
        client,
        config,
        topology,
        packet,
        state,
        (body, kind),
        (&mut accounting, &operation),
    )
    .await;
    // finish also fences senders accepted immediately before completion.
    let completion = operation
        .finish(Instant::now() + Duration::from_millis(config.cancel_cleanup_timeout_ms))
        .await;
    match (result, completion) {
        (Err(error), _) => {
            accounting.finish_if_unfinished(Outcome::RouterError);
            Err(error)
        }
        (Ok(()), Err(error)) => {
            accounting.finish(Outcome::RouterError);
            Err(error)
        }
        (Ok(()), Ok(())) => {
            release_idle_transaction(config, topology, state).await;
            Ok(())
        }
    }
}
async fn release_idle_transaction(
    config: &Config,
    topology: &watch::Receiver<Topology>,
    state: &mut SessionState,
) {
    if config.pool_mode == pool::Mode::Transaction && state.status == b'I' && !state.pinned {
        for backend in std::mem::take(&mut state.backends).into_values() {
            release_backend(config, backend, topology).await;
        }
    }
}

async fn deliver_query(
    client: &mut OwnedWriteHalf,
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    packet: &[u8],
    state: &mut SessionState,
    query: (&[u8], Option<Kind>),
    context: (&mut Guard, &cancel::Operation),
) -> Result<()> {
    let (accounting, operation) = context;
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
    let routed = match route_query(config, topology, packet, state, body, kind, operation).await {
        Ok(value) => value,
        Err(error) => {
            if matches!(operation.interrupted(), Some(cancel::Reason::User))
                && error.is::<cancel::Interrupted>()
            {
                operation
                    .retire(
                        Instant::now() + Duration::from_millis(config.cancel_cleanup_timeout_ms),
                    )
                    .await?;
                send(
                    client,
                    b'E',
                    b"SERROR\0C57014\0Mcanceling statement due to user request\0\0",
                )
                .await?;
                send(client, b'Z', &[state.status]).await?;
                accounting.finish(Outcome::SqlError);
                return Ok(());
            }
            if error.is::<pool::Rejected>() {
                fatal_query(
                    client,
                    "53300",
                    "rustybouncer: backend pool capacity/wait deadline exhausted",
                    accounting,
                )
                .await?;
                return Err(error);
            }
            if error.is::<buffers::Exhausted>() {
                fatal_query(client,"53200","rustybouncer: result capacity exhausted; dispatched statement outcome may be unknown; not replayed",accounting).await?;
                return Err(error);
            }
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
    operation
        .retire(Instant::now() + Duration::from_millis(config.cancel_cleanup_timeout_ms))
        .await?;
    let sequence = operation.sequence();
    let delivery = tokio::select! {
        biased;
        result=timeout(Duration::from_millis(config.delivery_timeout_ms),messages.write_to(client))=>result.context("frontend delivery timed out").and_then(|r|r),
        reason=operation.delivery_interrupted(sequence)=>Err(cancel::Interrupted(reason).into()),
    };
    if let Err(error) = delivery {
        accounting.finish(Outcome::DeliveryError);
        return Err(error);
    }
    accounting.finish(if sql_error {
        Outcome::SqlError
    } else {
        Outcome::Ok
    });
    Ok(())
}
async fn fatal_query<W: tokio::io::AsyncWrite + Unpin>(
    client: &mut W,
    code: &str,
    message: &str,
    accounting: &mut Guard,
) -> Result<()> {
    let result = timeout(Duration::from_secs(1), pg_error(client, code, message))
        .await
        .context("fatal error delivery timed out")
        .and_then(|r| r);
    accounting.finish(if result.is_ok() {
        Outcome::RouterError
    } else {
        Outcome::DeliveryError
    });
    result
}

async fn pooled_backend(
    config: &Config,
    node: usize,
    packet: Arc<[u8]>,
    selected: &Topology,
    deadline: Instant,
    ticket: &mut Option<Guard>,
) -> Result<Backend> {
    pool::checkout(config, node, packet, selected, deadline, ticket).await
}

async fn pooled_startup(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    packet: &[u8],
    ticket: &mut Option<Guard>,
) -> Result<(usize, Option<Backend>, Arc<Messages>)> {
    let deadline = Instant::now() + Duration::from_millis(config.wait_timeout_ms);
    loop {
        if Instant::now() >= deadline {
            bail!("startup routing budget exhausted");
        }
        let (node, selected) = wait_route(config, topology, false, &HashSet::new()).await?;
        let key = pool::Key::new(node, &selected, Arc::from(packet));
        let messages = if let Some(metadata) = config.pools.metadata(&key) {
            metadata
        } else {
            let backend =
                match pooled_backend(config, node, Arc::from(packet), &selected, deadline, ticket)
                    .await
                {
                    Ok(backend) => backend,
                    Err(error) if error.is::<pool::Stale>() || *topology.borrow() != selected => {
                        tokio::task::yield_now().await;
                        continue;
                    }
                    Err(error) => return Err(error),
                };
            let metadata = backend
                .startup_metadata
                .clone()
                .context("missing startup metadata")?;
            release_backend(config, backend, topology).await;
            metadata
        };
        if *topology.borrow() == selected {
            return Ok((node, None, messages));
        }
        tokio::task::yield_now().await;
    }
}

async fn release_backend(
    config: &Config,
    mut backend: Backend,
    topology: &watch::Receiver<Topology>,
) {
    let Some(slot) = &backend.slot else {
        return;
    };
    let key = slot.key.clone();
    if !key.eligible(&topology.borrow()) {
        config.metrics.inc("pool_discards_total", &[]);
        return;
    }
    backend.slot.as_mut().unwrap().phase(pool::Phase::Resetting);
    let mut accounting = config.metrics.guard(Event::PoolReset);
    let resetting = async {
        if backend.status != b'I' {
            let (response, status) = result(&mut backend, b"ROLLBACK\0").await?;
            if status != b'I' || response.sql_error {
                bail!("backend rollback failed");
            }
        }
        let (response, status) = result(&mut backend, b"DISCARD ALL\0").await?;
        let baseline = backend
            .startup_metadata
            .as_ref()
            .context("missing reset baseline")?;
        if status != b'I' || response.sql_error || !response.reset_parameters_match(baseline) {
            bail!("backend reset did not restore baseline");
        }
        if key.replica() {
            let (response, status) =
                result(&mut backend, b"SET default_transaction_read_only = on\0").await?;
            if status != b'I' || response.sql_error {
                bail!("replica reset failed");
            }
        }
        if !backend.healthy_idle() {
            bail!("unexpected backend data after reset");
        }
        Ok::<_, anyhow::Error>(())
    };
    let invalidated = async {
        let mut changed = topology.clone();
        loop {
            if !key.eligible(&changed.borrow_and_update()) {
                return;
            }
            if changed.changed().await.is_err() {
                return;
            }
        }
    };
    let good = tokio::select! {
        outcome=timeout(Duration::from_millis(config.pool_reset_timeout_ms), resetting)=>matches!(outcome, Ok(Ok(()))),
        _=invalidated=>false,
    };
    accounting.finish(if good { Outcome::Ok } else { Outcome::Error });
    if good {
        config.pools.put(&key, backend, &topology.borrow());
    } else {
        config.metrics.inc("pool_discards_total", &[]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn measured_config() -> Config {
        let mut c: Config = toml::from_str(include_str!("../rustybouncer.toml")).unwrap();
        c.metrics = Metrics::new(true, &c.nodes, true, c.max_clients).unwrap();
        c.cancellations = cancel::Registry::new(c.max_clients);
        c
    }
    fn text(m: &Metrics) -> String {
        String::from_utf8(m.render().unwrap()).unwrap()
    }
    #[tokio::test]
    async fn obsolete_undispatched_leases_close_without_discarding_session_or_transaction_state() {
        for (mode, transaction, pinned, status, close) in [
            (pool::Mode::None, false, false, b'I', false),
            (pool::Mode::Session, false, true, b'I', false),
            (pool::Mode::Transaction, true, false, b'T', false),
            (pool::Mode::Transaction, false, false, b'E', false),
            (pool::Mode::Transaction, false, false, b'I', true),
        ] {
            let (mut peer, stream) = crate::tests::socket_pair().await;
            let backend = Backend {
                stream: BufReader::new(stream),
                _live: Metrics::default().guard(Event::Backend("test".into())),
                generation: 1,
                buffers: buffers::Pool::default(),
                cancel_identity: (1, 2),
                status,
                startup_metadata: None,
                idle_since: Instant::now(),
                slot: None,
                pool_owner: None,
            };
            let mut state = SessionState {
                backends: HashMap::from([(0, backend)]),
                identity: Arc::from(&b"startup"[..]),
                transaction,
                status,
                pinned,
                primary_generation: 1,
            };
            state.discard_unassigned(mode);
            if close {
                assert_eq!(
                    timeout(Duration::from_secs(1), peer.read(&mut [0u8; 1]))
                        .await
                        .unwrap()
                        .unwrap(),
                    0
                );
            } else {
                assert!(
                    matches!(peer.try_read(&mut [0u8;1]),Err(error) if error.kind()==std::io::ErrorKind::WouldBlock)
                );
                assert_eq!(state.backends.len(), 1);
            }
        }
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
            identity: Arc::from(&b""[..]),
            transaction: false,
            status: b'I',
            pinned: false,
            primary_generation: 0,
        };
        let registration = c.cancellations.register().unwrap();
        let (_reader, mut server) = server.into_split();
        assert!(handle_query(
            &mut server,
            &c,
            &mut rx,
            &[],
            &mut state,
            (&body, None, c.metrics.guard(Event::Query("malformed"))),
            &registration.control
        )
        .await
        .is_err());
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
                buffers: c.result_buffers.clone(),
                cancel_identity: (1, 2),
                status: b'I',
                startup_metadata: None,
                idle_since: Instant::now(),
                slot: None,
                pool_owner: None,
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
            let registration = c.cancellations.register().unwrap();
            let operation = registration.control.begin().unwrap();
            let response = execute_query(
                &mut backend,
                b"SELECT 1\0",
                &mut rx,
                &topology,
                0,
                (true, Instant::now() + Duration::from_millis(20)),
                (&c, &operation),
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
                buffers: c.result_buffers.clone(),
                cancel_identity: (1, 2),
                status: b'I',
                startup_metadata: None,
                idle_since: Instant::now(),
                slot: None,
                pool_owner: None,
                _live: m.guard(Event::Backend("node-a".into())),
            };
            let selected = Topology {
                primary: Some(0),
                ..Topology::default()
            };
            let (tx, mut rx) = watch::channel(selected.clone());
            let task = tokio::spawn(async move {
                let mut backend = backend;
                let registration = c.cancellations.register().unwrap();
                let operation = registration.control.begin().unwrap();
                execute_query(
                    &mut backend,
                    b"SELECT 1\0",
                    &mut rx,
                    &selected,
                    0,
                    (true, Instant::now() + Duration::from_secs(60)),
                    (&c, &operation),
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
            assert!(text(&m).contains(&format!(
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
