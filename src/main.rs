use anyhow::{bail, Context, Result};
use serde::Deserialize;
use std::{collections::HashSet, env, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::{watch, Semaphore},
    task::JoinSet,
    time::{sleep, timeout, Instant},
};
use tokio_postgres::NoTls;
use tracing::{info, warn};
mod router;

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
    max_clients: usize,
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
    fn validate(&self) -> Result<()> {
        if self.nodes.is_empty() || self.max_clients == 0 || self.read_max_attempts == 0 {
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
    loop {
        let mut probes = JoinSet::new();
        for (index, node) in config.nodes.iter().cloned().enumerate() {
            let config = config.clone();
            let password = password.clone();
            probes.spawn(async move { (index, probe(&node, &config, password.as_deref()).await) });
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

async fn pg_error(client: &mut TcpStream, code: &str, message: &str) -> Result<()> {
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
                // Backend PID/secret belongs to a specific server. Routing it to
                // the current primary would be incorrect after a failover.
                bail!("cancel requests are unsupported in this prototype");
            }
            (196608, _) if packet.last() == Some(&0) => return Ok(packet),
            _ => bail!("unsupported PostgreSQL startup packet"),
        }
    }
    bail!("too many encryption negotiation requests")
}

async fn wait_backend(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
) -> Result<(TcpStream, Topology)> {
    let deadline = Instant::now() + Duration::from_millis(config.wait_timeout_ms);
    loop {
        let selected = topology.borrow_and_update().clone();
        if let Some(index) = selected.primary {
            let node = &config.nodes[index];
            let connect = timeout(
                Duration::from_millis(config.probe_timeout_ms),
                TcpStream::connect((node.host.as_str(), node.port)),
            );
            if let Ok(Ok(stream)) = connect.await {
                if *topology.borrow() == selected {
                    return Ok((stream, selected));
                }
            }
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
    if config.route_selects {
        return router::session(client, config, topology).await;
    }
    let packet = timeout(
        Duration::from_millis(config.startup_timeout_ms),
        startup(&mut client),
    )
    .await??;
    // An outer deadline bounds the entire wait, including slow DNS/connects.
    let backend = timeout(
        Duration::from_millis(config.wait_timeout_ms),
        wait_backend(&config, &mut topology),
    )
    .await;
    let (mut backend, selected) = match backend {
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
            return Ok(());
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
        } => { result?; }
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
    let config = Arc::new(config);
    let listener = TcpListener::bind(&config.listen).await?;
    let permits = Arc::new(Semaphore::new(config.max_clients));
    let (tx, rx) = watch::channel(Topology::default());
    let discovery = tokio::spawn(discover(config.clone(), password, tx));
    info!(listen = %config.listen, "rustybouncer ready");
    let mut sessions = JoinSet::new();
    loop {
        tokio::select! {
            _ = tokio::signal::ctrl_c() => break,
            result = sessions.join_next(), if !sessions.is_empty() => {
                if let Some(Err(error)) = result { warn!(%error, "session task failed"); }
            }
            accepted = listener.accept() => {
                let (stream, peer) = accepted?;
                match permits.clone().try_acquire_owned() {
                    Ok(permit) => {
                        let config = config.clone();
                        let topology = rx.clone();
                        sessions.spawn(async move {
                            let _permit = permit;
                            if let Err(error) = session(stream, config, topology).await {
                                warn!(%peer, %error, "session ended");
                            }
                        });
                    }
                    Err(_) => { warn!(%peer, "client capacity reached; connection closed"); }
                }
            }
        }
    }
    discovery.abort();
    sessions.abort_all();
    while sessions.join_next().await.is_some() {}
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

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

    async fn socket_pair() -> (TcpStream, TcpStream) {
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
        assert!(
            timeout(Duration::from_secs(1), wait_backend(&config, &mut rx))
                .await
                .unwrap()
                .is_err()
        );
    }
}
