use anyhow::{bail, Context, Result};
use serde::Deserialize;
use std::{collections::HashSet, env, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWrite, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::{watch, Semaphore},
    task::JoinSet,
    time::{sleep, timeout, Instant},
};
use tokio_postgres::NoTls;
use tracing::{info, warn};
mod buffers;
mod cancel;
mod metrics;
mod router;
use metrics::{Event, Guard, Metrics, Outcome};

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Node {
    name: String,
    host: String,
    port: u16,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    listen: String,
    metrics_listen: Option<String>,
    #[serde(skip)]
    metrics: Metrics,
    max_clients: usize,
    #[serde(default)]
    pool_mode: router::pool::Mode,
    #[serde(default = "default_pool_size")]
    pool_size: usize,
    #[serde(default = "default_pool_backends")]
    max_pool_backends: usize,
    #[serde(default)]
    reserve_pool_size: usize,
    #[serde(default = "default_pool_delay")]
    reserve_pool_delay_ms: u64,
    #[serde(default = "default_pool_idle")]
    pool_idle_timeout_ms: u64,
    #[serde(default = "default_pool_lifetime")]
    pool_server_lifetime_ms: u64,
    #[serde(default = "default_cancel_cleanup")]
    pool_reset_timeout_ms: u64,
    #[serde(skip)]
    pools: router::pool::Manager,

    #[serde(default = "default_result_buffer_bytes")]
    result_buffer_bytes: usize,
    #[serde(skip)]
    result_buffers: buffers::Pool,
    #[serde(skip)]
    cancellations: cancel::Registry,
    #[serde(default = "default_cancel_cleanup")]
    cancel_cleanup_timeout_ms: u64,
    #[serde(default = "default_delivery_timeout")]
    delivery_timeout_ms: u64,
    probe_interval_ms: u64,
    probe_timeout_ms: u64,
    wait_timeout_ms: u64,
    startup_timeout_ms: u64,
    #[serde(default = "default_query_timeout")]
    query_timeout_ms: u64,
    #[serde(default = "default_retry_timeout")]
    read_retry_timeout_ms: u64,
    #[serde(default = "default_read_attempts")]
    read_max_attempts: usize,
    probe_user: String,
    probe_database: String,
    probe_password_env: Option<String>,
    nodes: Vec<Node>,
    #[serde(default)]
    route_selects: bool,
}
fn default_pool_size() -> usize {
    16
}
fn default_pool_backends() -> usize {
    64
}
fn default_pool_delay() -> u64 {
    1000
}
fn default_pool_idle() -> u64 {
    30000
}
fn default_pool_lifetime() -> u64 {
    300000
}
fn default_cancel_cleanup() -> u64 {
    5000
}
fn default_result_buffer_bytes() -> usize {
    buffers::DEFAULT_LIMIT
}
fn default_delivery_timeout() -> u64 {
    30_000
}
fn default_query_timeout() -> u64 {
    300_000
}
fn default_retry_timeout() -> u64 {
    600_000
}
fn default_read_attempts() -> usize {
    3
}

impl Config {
    fn pool_settings(&self) -> router::pool::Settings {
        router::pool::Settings {
            mode: self.pool_mode,
            size: self.pool_size,
            global: self.max_pool_backends,
            reserve: self.reserve_pool_size,
            reserve_delay: Duration::from_millis(self.reserve_pool_delay_ms),
            waiters: self.max_clients,
            idle: Duration::from_millis(self.pool_idle_timeout_ms),
            lifetime: Duration::from_millis(self.pool_server_lifetime_ms),
        }
    }
    fn validate(&self) -> Result<()> {
        if self.nodes.is_empty()
            || self.max_clients == 0
            || self.max_clients > Semaphore::MAX_PERMITS
            || self.read_max_attempts == 0
        {
            bail!("at least one node and a positive max_clients are required");
        }
        if [
            self.probe_interval_ms,
            self.probe_timeout_ms,
            self.wait_timeout_ms,
            self.startup_timeout_ms,
            self.query_timeout_ms,
            self.read_retry_timeout_ms,
        ]
        .contains(&0)
        {
            bail!("timeouts and probe interval must be positive");
        }
        self.pool_settings().validate()?;
        if self.pool_mode != router::pool::Mode::None && !self.route_selects {
            bail!("shared pools require routed trust-authenticated mode");
        }
        if self.pool_reset_timeout_ms == 0 {
            bail!("pool reset timeout must be positive");
        }
        buffers::Pool::new(self.result_buffer_bytes, Metrics::default())?;
        if self.delivery_timeout_ms == 0 || self.cancel_cleanup_timeout_ms == 0 {
            bail!("delivery timeout must be positive");
        }
        let mut names = HashSet::new();
        let mut addresses = HashSet::new();
        for node in &self.nodes {
            if node.name.is_empty() || node.host.is_empty() || node.port == 0 {
                bail!("node name, host and port must be nonempty");
            }
            if !names.insert(&node.name) || !addresses.insert((&node.host, node.port)) {
                bail!("duplicate node name or address");
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct Topology {
    primary: Option<usize>,
    replicas: Vec<usize>,
    ambiguous: bool,
    generation: u64,
}

fn unique_primary(writable: &[usize]) -> Option<usize> {
    match writable {
        [one] => Some(*one),
        _ => None,
    }
}

async fn probe(node: &Node, config: &Config, password: Option<&str>) -> Option<(bool, bool)> {
    let result = timeout(Duration::from_millis(config.probe_timeout_ms), async {
        let mut pg = tokio_postgres::Config::new();
        pg.host(&node.host)
            .port(node.port)
            .user(&config.probe_user)
            .dbname(&config.probe_database)
            .connect_timeout(Duration::from_millis(config.probe_timeout_ms));
        if let Some(password) = password {
            pg.password(password);
        }
        let (client, connection) = pg.connect(NoTls).await?;
        // Abort on timeout too: dropping JoinHandle alone would detach the driver.
        struct Driver(tokio::task::JoinHandle<()>);
        impl Drop for Driver {
            fn drop(&mut self) {
                self.0.abort();
            }
        }
        let _driver = Driver(tokio::spawn(async move {
            let _ = connection.await;
        }));
        let row = client
            .query_one(
                "SELECT pg_is_in_recovery(), current_setting('transaction_read_only') = 'off'",
                &[],
            )
            .await?;
        Ok::<(bool, bool), tokio_postgres::Error>((row.get(0), row.get(1)))
    })
    .await;
    result.ok().and_then(|result| result.ok())
}

async fn discover(config: Arc<Config>, password: Option<String>, tx: watch::Sender<Topology>) {
    let mut discovered = false;
    loop {
        let mut probes = JoinSet::new();
        for (index, node) in config.nodes.iter().cloned().enumerate() {
            let config = config.clone();
            let password = password.clone();
            probes.spawn(async move {
                let start = std::time::Instant::now();
                let result = probe(&node, &config, password.as_deref()).await;
                config.metrics.inc(
                    "probes_total",
                    &[&node.name, if result.is_some() { "ok" } else { "error" }],
                );
                config.metrics.observe(
                    "probe_duration_seconds",
                    &[&node.name],
                    start.elapsed().as_secs_f64(),
                );
                (index, result)
            });
        }
        let mut writable = Vec::new();
        let mut replicas = Vec::new();
        while let Some(result) = probes.join_next().await {
            if let Ok((index, Some((recovery, writable_node)))) = result {
                if recovery {
                    replicas.push(index);
                } else if writable_node {
                    writable.push(index);
                }
            }
        }
        replicas.sort_unstable();
        let primary = unique_primary(&writable);
        let ambiguous = writable.len() > 1;
        if writable.len() > 1 {
            warn!("multiple writable nodes detected; routing disabled");
        }
        let previous = tx.borrow().clone();
        let changed = previous.primary != primary
            || previous.replicas != replicas
            || previous.ambiguous != ambiguous;
        let observed = Topology {
            primary,
            replicas: replicas.clone(),
            ambiguous,
            generation: previous.generation,
        };
        config.metrics.topology(
            &config.nodes,
            &observed,
            changed || !discovered,
            previous.primary != primary,
        );
        discovered = true;
        if previous.primary != primary
            || previous.replicas != replicas
            || previous.ambiguous != ambiguous
        {
            let next = Topology {
                primary,
                replicas,
                ambiguous,
                generation: previous.generation
                    + u64::from(previous.primary != primary || previous.ambiguous != ambiguous),
            };
            info!(?next, "topology changed");
            tx.send_replace(next);
        }
        sleep(Duration::from_millis(config.probe_interval_ms)).await;
    }
}

async fn pg_error<W: AsyncWrite + Unpin>(client: &mut W, code: &str, message: &str) -> Result<()> {
    let mut body = Vec::new();
    for (key, value) in [(b'S', "FATAL"), (b'C', code), (b'M', message)] {
        body.push(key);
        body.extend_from_slice(value.as_bytes());
        body.push(0);
    }
    body.push(0);
    client.write_u8(b'E').await?;
    client.write_u32((body.len() + 4) as u32).await?;
    client.write_all(&body).await?;
    Ok(())
}

// Consume only the startup negotiation; authentication and all later protocol
// messages pass through untouched, including extended queries and COPY.
async fn startup(client: &mut TcpStream) -> Result<Vec<u8>> {
    for _ in 0..3 {
        let length = client.read_u32().await?;
        if !(8..=65536).contains(&length) {
            bail!("invalid startup packet length");
        }
        let mut packet = vec![0; length as usize];
        packet[..4].copy_from_slice(&length.to_be_bytes());
        client.read_exact(&mut packet[4..]).await?;
        let code = u32::from_be_bytes(packet[4..8].try_into().unwrap());
        match (code, length) {
            (80877103 | 80877104, 8) => client.write_all(b"N").await?,
            (80877102, 16) => {
                return Ok(packet);
            }
            (196608, _) if packet.last() == Some(&0) => return Ok(packet),
            _ => bail!("unsupported PostgreSQL startup packet"),
        }
    }
    bail!("too many encryption negotiation requests")
}

async fn connect_tcp(
    config: &Config,
    index: usize,
    ticket: &mut Option<Guard>,
) -> Result<(TcpStream, Guard)> {
    let node = &config.nodes[index];
    *ticket = Some(config.metrics.guard(Event::Connect(node.name.clone())));
    let result = timeout(
        Duration::from_millis(config.probe_timeout_ms),
        TcpStream::connect((node.host.as_str(), node.port)),
    )
    .await;
    finish_ticket(
        ticket,
        if matches!(result, Ok(Ok(_))) {
            Outcome::Ok
        } else {
            Outcome::Error
        },
    );
    let stream = result??;
    Ok((
        stream,
        config.metrics.guard(Event::Backend(node.name.clone())),
    ))
}
fn finish_ticket(ticket: &mut Option<Guard>, outcome: Outcome) {
    if let Some(mut guard) = ticket.take() {
        guard.finish(outcome);
    }
}

async fn wait_backend(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    ticket: &mut Option<Guard>,
) -> Result<(TcpStream, Topology, Guard)> {
    let mut wait = Some(config.metrics.timer("route_wait_duration_seconds"));
    let deadline = Instant::now() + Duration::from_millis(config.wait_timeout_ms);
    loop {
        let selected = topology.borrow_and_update().clone();
        if let Some(index) = selected.primary {
            drop(wait.take());
            if let Ok((stream, live)) = connect_tcp(config, index, ticket).await {
                if *topology.borrow() == selected {
                    return Ok((stream, selected, live));
                }
            }
        }
        if wait.is_none() {
            wait = Some(config.metrics.timer("route_wait_duration_seconds"));
        }
        if Instant::now() >= deadline {
            bail!("timed out waiting for a writable primary");
        }
        tokio::select! {
            changed = topology.changed() => { changed.context("discovery stopped")?; }
            _ = sleep(Duration::from_millis(50)) => {}
        }
    }
}

async fn session(
    mut client: TcpStream,
    config: Arc<Config>,
    mut topology: watch::Receiver<Topology>,
) -> Result<()> {
    let packet = timeout(
        Duration::from_millis(config.startup_timeout_ms),
        startup(&mut client),
    )
    .await??;
    if cancel_request(&packet) {
        deliver_cancel(&config, &packet).await?;
        return Ok(());
    }
    if config.route_selects {
        return router::session(client, config, topology, packet).await;
    }
    // An outer deadline bounds the entire wait, including slow DNS/connects.
    let mut ticket = None;
    let backend = timeout(
        Duration::from_millis(config.wait_timeout_ms),
        wait_backend(&config, &mut topology, &mut ticket),
    )
    .await;
    finish_ticket(&mut ticket, Outcome::Error);
    let (mut backend, selected, _live) = match backend {
        Ok(Ok(value)) => value,
        _ => {
            timeout(
                Duration::from_secs(1),
                pg_error(
                    &mut client,
                    "08006",
                    "rustybouncer: no unambiguous writable primary available",
                ),
            )
            .await??;
            bail!("startup unavailable");
        }
    };
    client.set_nodelay(true)?;
    backend.set_nodelay(true)?;
    // From this point on, never retry: even a partial write has uncertain outcome.
    tokio::select! {
        result = async {
            loop {
                topology.changed().await?;
                if topology.borrow().generation != selected.generation { break; }
            }
            Ok::<(), watch::error::RecvError>(())
        } => { result?; bail!("topology changed during passthrough session"); }
        result = async {
            backend.write_all(&packet).await?;
            tokio::io::copy_bidirectional(&mut client, &mut backend).await?;
            Ok::<(), std::io::Error>(())
        } => { result?; }
    }
    info!(
        generation = selected.generation,
        "session closed; no statements replayed"
    );
    Ok(())
}

fn cancel_request(packet: &[u8]) -> bool {
    packet.len() == 16 && packet[4..8] == 80877102u32.to_be_bytes()
}
async fn deliver_cancel(config: &Config, packet: &[u8]) -> Result<()> {
    config.metrics.inc("control_requests_total", &[]);
    let pid = u32::from_be_bytes(packet[8..12].try_into().unwrap());
    let secret = u32::from_be_bytes(packet[12..16].try_into().unwrap());
    config
        .cancellations
        .request(
            pid,
            secret,
            Instant::now() + Duration::from_millis(config.cancel_cleanup_timeout_ms),
        )
        .await?;
    Ok(())
}
// Reserved handshake slots let cancellation work at full SQL-client capacity.
// Overflow never consumes SQL capacity or creates an unbounded task queue.
async fn control_session(mut stream: TcpStream, config: Arc<Config>) -> Result<()> {
    let incoming = timeout(
        Duration::from_millis(config.startup_timeout_ms),
        startup(&mut stream),
    )
    .await;
    match incoming {
        Ok(Ok(packet)) if cancel_request(&packet) => deliver_cancel(&config, &packet).await,
        _ => {
            config.metrics.inc("clients_accepted_total", &[]);
            config.metrics.inc("clients_rejected_total", &[]);
            Ok(())
        }
    }
}

async fn accounted_session(
    mut accounting: Guard,
    stream: TcpStream,
    config: Arc<Config>,
    topology: watch::Receiver<Topology>,
) -> Result<()> {
    let result = session(stream, config, topology).await;
    accounting.finish(if result.is_ok() {
        Outcome::Ok
    } else {
        Outcome::Error
    });
    result
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "rustybouncer=info".into()),
        )
        .init();
    let path = env::args()
        .nth(1)
        .unwrap_or_else(|| "rustybouncer.toml".into());
    let config: Config =
        toml::from_str(&std::fs::read_to_string(&path).context("reading config")?)?;
    config.validate()?;
    let password = config
        .probe_password_env
        .as_ref()
        .map(|key| env::var(key).with_context(|| format!("missing environment variable {key}")))
        .transpose()?;
    let mut config = config;
    config.metrics = Metrics::new(
        config.metrics_listen.is_some(),
        &config.nodes,
        config.route_selects,
        config.max_clients,
    )?;
    config.pools = router::pool::Manager::new(config.pool_settings(), config.metrics.clone())?;
    config.metrics.configure_pool(
        config.pool_mode.label(),
        config.max_pool_backends,
        config.pool_size,
    );
    config.cancellations = cancel::Registry::new(config.max_clients);
    config.result_buffers = buffers::Pool::new(config.result_buffer_bytes, config.metrics.clone())?;
    let metrics_listener = match &config.metrics_listen {
        Some(address) => Some(
            TcpListener::bind(address)
                .await
                .context("binding metrics listener")?,
        ),
        None => None,
    };
    let config = Arc::new(config);
    let mut metrics_task = metrics_listener
        .map(|listener| tokio::spawn(metrics::serve(listener, config.metrics.clone())));
    let listener = TcpListener::bind(&config.listen).await?;
    let permits = Arc::new(Semaphore::new(config.max_clients));
    let control_permits = Arc::new(Semaphore::new(32));
    let (tx, rx) = watch::channel(Topology::default());
    let discovery = tokio::spawn(discover(config.clone(), password, tx));
    let maintenance = if config.pool_mode != router::pool::Mode::None {
        let pools = config.pools.clone();
        let topology = rx.clone();
        Some(tokio::spawn(async move {
            loop {
                pools.maintain(&topology.borrow());
                sleep(Duration::from_millis(100)).await;
            }
        }))
    } else {
        None
    };
    info!(listen = %config.listen, "rustybouncer ready");
    let mut sessions = JoinSet::new();
    // Keep one registered receiver across admission/completion selections.
    let shutdown = tokio::signal::ctrl_c();
    tokio::pin!(shutdown);
    loop {
        while let Some(result) = sessions.try_join_next() {
            if let Err(error) = result {
                warn!(%error,"session task failed");
            }
        }
        tokio::select! {
            result = async { metrics_task.as_mut().unwrap().await }, if metrics_task.is_some() => {
                discovery.abort();
                if let Some(task)=&maintenance {task.abort();}
                sessions.abort_all();
                return metrics::listener_exit(result);
            }
            _ = &mut shutdown => break,
            result = sessions.join_next(), if !sessions.is_empty() => {
                if let Some(Err(error)) = result { warn!(%error, "session task failed"); }
            }
            accepted = listener.accept() => {
                let (stream, peer) = accepted?;

                match permits.clone().try_acquire_owned() {
                    Ok(permit) => {
                        config.metrics.inc("clients_accepted_total", &[]);
                        let config = config.clone();
                        let topology = rx.clone();
                        let accounting = config.metrics.guard(Event::Session);
                        sessions.spawn(async move {
                            let _permit = permit;
                            if let Err(error) = accounted_session(accounting,stream,config,topology).await {
                                warn!(%peer, %error, "session ended");
                            }
                        });
                    }
                    Err(_) => {
                        if let Ok(permit)=control_permits.clone().try_acquire_owned() {
                            let config=config.clone();
                            sessions.spawn(async move {
                                let _permit=permit;
                                if let Err(error)=control_session(stream,config).await {warn!(%peer,%error,"control connection ended");}
                            });
                        } else {
                            config.metrics.inc("clients_accepted_total",&[]);
                            config.metrics.inc("clients_rejected_total",&[]);
                        }
                    }
                }
            }
        }
    }
    if let Some(task) = metrics_task {
        task.abort();
    }
    discovery.abort();
    if let Some(task) = maintenance {
        task.abort();
        let _ = task.await;
    }
    sessions.abort_all();
    while sessions.join_next().await.is_some() {}
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn fatal_startup_is_session_error_even_after_successful_delivery() {
        for routed in [false, true] {
            let mut c: Config = toml::from_str(include_str!("../rustybouncer.toml")).unwrap();
            c.route_selects = routed;
            c.wait_timeout_ms = 10;
            c.metrics = Metrics::new(true, &c.nodes, routed, c.max_clients).unwrap();
            let m = c.metrics.clone();
            let (_tx, rx) = watch::channel(Topology::default());
            let (mut client, server) = socket_pair().await;
            let guard = m.guard(Event::Session);
            let task = tokio::spawn(accounted_session(guard, server, Arc::new(c), rx));
            client
                .write_all(&[0, 0, 0, 9, 0, 3, 0, 0, 0])
                .await
                .unwrap();
            assert_eq!(client.read_u8().await.unwrap(), b'E');
            assert!(task.await.unwrap().is_err());
            let text = String::from_utf8(m.render().unwrap()).unwrap();
            assert!(text.contains("rustybouncer_sessions_completed_total{outcome=\"error\"} 1"));
            assert!(text.contains("rustybouncer_clients_active 0"));
        }
    }
    #[tokio::test]
    async fn tcp_connect_failure_has_no_live_backend_and_one_error() {
        let mut c: Config = toml::from_str(include_str!("../rustybouncer.toml")).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        c.nodes[0].port = listener.local_addr().unwrap().port();
        drop(listener);
        c.metrics = Metrics::new(true, &c.nodes, true, 100).unwrap();
        assert!(connect_tcp(&c, 0, &mut None).await.is_err());
        let text = String::from_utf8(c.metrics.render().unwrap()).unwrap();
        assert!(text
            .contains("rustybouncer_backend_connects_total{node=\"node-a\",outcome=\"error\"} 1"));
        assert!(text.contains("rustybouncer_backend_connections_active{node=\"node-a\"} 0"));
    }
    #[test]
    fn primary_selection_fails_closed() {
        assert_eq!(unique_primary(&[]), None);
        assert_eq!(unique_primary(&[1]), Some(1));
        assert_eq!(unique_primary(&[0, 1]), None);
    }

    #[test]
    fn validates_config() {
        let config: Config = toml::from_str(include_str!("../rustybouncer.toml")).unwrap();
        config.validate().unwrap();
        assert_eq!(config.read_max_attempts, 3);
        assert_eq!(config.result_buffer_bytes, buffers::DEFAULT_LIMIT);
        assert_eq!(config.delivery_timeout_ms, 30_000);
        assert_eq!(config.cancel_cleanup_timeout_ms, 5_000);
        for change in [0, 1, 2, 3] {
            let mut bad = config.clone();
            match change {
                0 => bad.result_buffer_bytes = 511,
                1 => bad.delivery_timeout_ms = 0,
                2 => bad.cancel_cleanup_timeout_ms = 0,
                _ => bad.max_clients = usize::MAX,
            }
            assert!(bad.validate().is_err());
        }
        let legacy = include_str!("../rustybouncer.toml")
            .lines()
            .filter(|line| {
                !line.starts_with("query_timeout_ms")
                    && !line.starts_with("read_retry_timeout_ms")
                    && !line.starts_with("read_max_attempts")
            })
            .collect::<Vec<_>>()
            .join("\n");
        let legacy: Config = toml::from_str(&legacy).unwrap();
        legacy.validate().unwrap();
        assert_eq!(legacy.query_timeout_ms, 300_000);
        assert_eq!(legacy.read_retry_timeout_ms, 600_000);
        assert_eq!(legacy.read_max_attempts, 3);
        let mut invalid = config.clone();
        invalid.read_max_attempts = 0;
        assert!(invalid.validate().is_err());
        invalid = config.clone();
        invalid.nodes.push(invalid.nodes[0].clone());
        assert!(invalid.validate().is_err());
        invalid = config;
        invalid.wait_timeout_ms = 0;
        assert!(invalid.validate().is_err());
    }

    pub(crate) async fn socket_pair() -> (TcpStream, TcpStream) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let client = TcpStream::connect(listener.local_addr().unwrap())
            .await
            .unwrap();
        (client, listener.accept().await.unwrap().0)
    }

    #[tokio::test]
    async fn startup_declines_tls_and_preserves_packet() {
        let (mut client, mut server) = socket_pair().await;
        let task = tokio::spawn(async move { startup(&mut server).await.unwrap() });
        client
            .write_all(&[0, 0, 0, 8, 4, 210, 22, 47])
            .await
            .unwrap();
        assert_eq!(client.read_u8().await.unwrap(), b'N');
        let packet = [0, 0, 0, 9, 0, 3, 0, 0, 0];
        client.write_all(&packet).await.unwrap();
        assert_eq!(task.await.unwrap(), packet);
    }

    #[tokio::test]
    async fn startup_rejects_oversized_packet() {
        let (mut client, mut server) = socket_pair().await;
        client.write_u32(65537).await.unwrap();
        assert!(startup(&mut server).await.is_err());
    }

    #[tokio::test]
    async fn backend_wait_is_bounded() {
        let mut config: Config = toml::from_str(include_str!("../rustybouncer.toml")).unwrap();
        config.wait_timeout_ms = 20;
        let (_tx, mut rx) = watch::channel(Topology::default());
        assert!(timeout(
            Duration::from_secs(1),
            wait_backend(&config, &mut rx, &mut None)
        )
        .await
        .unwrap()
        .is_err());
    }
}
