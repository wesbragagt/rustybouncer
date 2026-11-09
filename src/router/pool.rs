//! Exclusive process-owned backends, with bounded FIFO capacity admission.
//! Idle backends hold weak slot owners; a strong idle cycle would leak the pool.
use super::*;
use std::{
    collections::VecDeque,
    sync::{Mutex, Weak},
};
use tokio::sync::Notify;

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Mode {
    #[default]
    None,
    Session,
    Transaction,
}
impl Mode {
    pub fn label(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Session => "session",
            Self::Transaction => "transaction",
        }
    }
}

#[derive(Clone)]
pub(crate) struct Settings {
    pub mode: Mode,
    pub size: usize,
    pub global: usize,
    pub reserve: usize,
    pub reserve_delay: Duration,
    pub waiters: usize,
    pub idle: Duration,
    pub lifetime: Duration,
}
impl Settings {
    pub fn validate(&self) -> Result<()> {
        if self.size == 0
            || self.global == 0
            || self.global > i64::MAX as usize
            || self.waiters == 0
            || self
                .size
                .checked_add(self.reserve)
                .is_none_or(|sum| sum > self.global)
        {
            bail!(
                "pool size/waiters must be positive and pool plus reserve must fit global capacity"
            );
        }
        if self.idle.is_zero() || self.lifetime.is_zero() || self.reserve_delay.is_zero() {
            bail!("pool retirement/reserve durations must be positive");
        }
        Ok(())
    }
}

#[derive(Clone, Hash, PartialEq, Eq)]
pub(super) struct Key {
    node: usize,
    generation: u64,
    replica: bool,
    packet: Arc<[u8]>,
}
impl Key {
    pub fn new(node: usize, selected: &Topology, packet: Arc<[u8]>) -> Self {
        Self {
            node,
            generation: selected.generation,
            replica: selected.replicas.contains(&node),
            packet,
        }
    }
    pub fn replica(&self) -> bool {
        self.replica
    }
    fn eligible(&self, topology: &Topology) -> bool {
        !topology.ambiguous
            && self.generation == topology.generation
            && if self.replica {
                topology.replicas.contains(&self.node)
            } else {
                topology.primary == Some(self.node)
            }
    }
}

struct Waiting {
    id: u64,
    since: Instant,
}
#[derive(Default)]
struct Group {
    total: usize,
    idle: VecDeque<Backend>,
    waiting: VecDeque<Waiting>,
    metadata: Option<Arc<Messages>>,
    invalid: bool,
}
struct State {
    groups: HashMap<Key, Group>,
    total: usize,
    waiting: usize,
    next: u64,
}
struct Inner {
    state: Mutex<State>,
    settings: Settings,
    changed: Notify,
    metrics: Metrics,
}
#[derive(Clone, Default)]
pub(crate) struct Manager(Option<Arc<Inner>>);
impl Manager {
    pub fn new(settings: Settings, metrics: Metrics) -> Result<Self> {
        settings.validate()?;
        if settings.mode == Mode::None {
            return Ok(Self::default());
        }
        Ok(Self(Some(Arc::new(Inner {
            state: Mutex::new(State {
                groups: HashMap::new(),
                total: 0,
                waiting: 0,
                next: 1,
            }),
            settings,
            changed: Notify::new(),
            metrics,
        }))))
    }
    pub(super) fn metadata(&self, key: &Key) -> Option<Arc<Messages>> {
        self.0
            .as_ref()?
            .state
            .lock()
            .unwrap()
            .groups
            .get(key)?
            .metadata
            .clone()
    }
    pub(super) fn publish(&self, key: &Key, metadata: Arc<Messages>) {
        if let Some(inner) = &self.0 {
            if let Some(group) = inner.state.lock().unwrap().groups.get_mut(key) {
                group.metadata = Some(metadata);
            }
        }
    }
    fn ticket(&self, key: &Key) -> Result<Ticket> {
        let inner = self.0.as_ref().context("backend pool disabled")?;
        let mut state = inner.state.lock().unwrap();
        if state.waiting >= inner.settings.waiters {
            return Err(Rejected("backend waiter capacity exhausted").into());
        }
        state.groups.try_reserve(1)?;
        let id = state.next;
        state.next = state
            .next
            .checked_add(1)
            .context("backend admission identity exhausted")?;
        state
            .groups
            .entry(key.clone())
            .or_default()
            .waiting
            .try_reserve(1)?;
        let group = state.groups.get_mut(key).unwrap();
        group.invalid = false; // New callers carry a freshly selected eligible key.
        group.waiting.push_back(Waiting {
            id,
            since: Instant::now(),
        });
        state.waiting += 1;
        Ok(Ticket {
            owner: Arc::downgrade(inner),
            key: key.clone(),
            id,
            active: true,
        })
    }
    pub(super) async fn acquire(&self, key: Key, deadline: Instant) -> Result<Checkout> {
        let inner = self.0.as_ref().context("backend pool disabled")?;
        let mut ticket = self.ticket(&key)?;
        loop {
            if Instant::now() >= deadline {
                return Err(Rejected("backend pool wait timed out").into());
            }
            let notified = inner.changed.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            let (decision, garbage, wake) = self.decide(&mut ticket)?;
            drop(garbage); // Slot destructors take the metadata lock.
            if let Some(mut choice) = decision {
                if let Checkout::Idle(backend) = &mut choice {
                    backend.pool_owner = Some(self.clone());
                }
                return Ok(choice);
            }
            if Instant::now() >= deadline {
                return Err(Rejected("backend pool wait timed out").into());
            }
            tokio::select! {
                _=notified=>{},
                _=tokio::time::sleep_until(wake.min(deadline))=>{},
            }
        }
    }
    fn decide(&self, ticket: &mut Ticket) -> Result<(Option<Checkout>, Vec<Backend>, Instant)> {
        let inner = self.0.as_ref().unwrap();
        let mut state = inner.state.lock().unwrap();
        let now = Instant::now();
        if state
            .groups
            .get(&ticket.key)
            .is_none_or(|group| group.invalid)
        {
            return Err(Stale.into());
        }
        let first = state
            .groups
            .iter()
            .filter_map(|(key, group)| {
                let head = group.waiting.front()?;
                let limit = inner.settings.size
                    + if now.duration_since(head.since) >= inner.settings.reserve_delay {
                        inner.settings.reserve
                    } else {
                        0
                    };
                (!group.invalid && (!group.idle.is_empty() || group.total < limit))
                    .then_some((head.id, key.clone()))
            })
            .min_by_key(|(id, _)| *id);
        let mut garbage = Vec::new();
        if first.as_ref().is_some_and(|(id, _)| *id == ticket.id) {
            let idle = state.groups.get_mut(&ticket.key).unwrap().idle.pop_front();
            if let Some(backend) = idle {
                ticket.remove(&mut state);
                return Ok((Some(Checkout::Idle(Box::new(backend))), garbage, now));
            }
            if state.total < inner.settings.global {
                state.total += 1;
                state.groups.get_mut(&ticket.key).unwrap().total += 1;
                ticket.remove(&mut state);
                let slot = Slot {
                    owner: Arc::downgrade(inner),
                    key: ticket.key.clone(),
                    created: now,
                };
                return Ok((Some(Checkout::New(slot)), garbage, now));
            }
            // Evict only idle ownership. Active/resetting work is never displaced.
            if let Some(key) = state
                .groups
                .iter()
                .filter(|(_, group)| !group.idle.is_empty())
                .min_by_key(|(_, group)| group.idle.front().unwrap().idle_since)
                .map(|(key, _)| key.clone())
            {
                garbage.push(
                    state
                        .groups
                        .get_mut(&key)
                        .unwrap()
                        .idle
                        .pop_front()
                        .unwrap(),
                );
            }
        }
        let wake = state
            .groups
            .get(&ticket.key)
            .and_then(|group| group.waiting.front())
            .map(|head| head.since + inner.settings.reserve_delay)
            .filter(|time| *time > now)
            .unwrap_or(now + Duration::from_secs(3600));
        Ok((None, garbage, wake))
    }
    pub(super) fn put(&self, key: &Key, mut backend: Backend, topology: &Topology) -> bool {
        let Some(inner) = &self.0 else {
            return false;
        };
        if !key.eligible(topology)
            || backend
                .slot
                .as_ref()
                .is_none_or(|slot| slot.created.elapsed() >= inner.settings.lifetime)
        {
            inner.metrics.inc("pool_discards_total", &[]);
            return false;
        }
        // Config/the caller still owns self. Idle entries must not own it strongly.
        drop(backend.pool_owner.take());
        backend.idle_since = Instant::now();
        let mut state = inner.state.lock().unwrap();
        let group = state.groups.get_mut(key).unwrap();
        if group.invalid {
            drop(state);
            inner.metrics.inc("pool_discards_total", &[]);
            return false;
        }
        group.idle.push_back(backend);
        drop(state);
        inner.changed.notify_waiters();
        true
    }
    pub fn maintain(&self, topology: &Topology) {
        let Some(inner) = &self.0 else {
            return;
        };
        let mut garbage = Vec::new();
        {
            let mut state = inner.state.lock().unwrap();
            for (key, group) in &mut state.groups {
                group.invalid = !key.eligible(topology);
                let mut kept = VecDeque::new();
                while let Some(backend) = group.idle.pop_front() {
                    let expired = backend.idle_since.elapsed() >= inner.settings.idle
                        || backend
                            .slot
                            .as_ref()
                            .is_none_or(|slot| slot.created.elapsed() >= inner.settings.lifetime);
                    if group.invalid || expired || !backend.healthy_idle() {
                        garbage.push(backend);
                    } else {
                        kept.push_back(backend);
                    }
                }
                group.idle = kept;
            }
        }
        for _ in 0..garbage.len() {
            inner.metrics.inc("pool_discards_total", &[]);
        }
        drop(garbage);
        inner.changed.notify_waiters();
    }
    #[cfg(test)]
    fn snapshot(&self) -> (usize, usize) {
        let state = self.0.as_ref().unwrap().state.lock().unwrap();
        (state.total, state.waiting)
    }
}

#[derive(Debug)]
pub(super) struct Rejected(pub &'static str);
impl std::fmt::Display for Rejected {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for Rejected {}
#[derive(Debug)]
pub(super) struct Stale;
impl std::fmt::Display for Stale {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("backend pool generation is stale")
    }
}
impl std::error::Error for Stale {}

pub(super) enum Checkout {
    Idle(Box<Backend>),
    New(Slot),
}
pub(super) struct Slot {
    owner: Weak<Inner>,
    pub key: Key,
    created: Instant,
}
impl Drop for Slot {
    fn drop(&mut self) {
        if let Some(inner) = self.owner.upgrade() {
            let mut state = inner
                .state
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            state.total -= 1;
            let group = state.groups.get_mut(&self.key).unwrap();
            group.total -= 1;
            if group.total == 0 && group.waiting.is_empty() {
                state.groups.remove(&self.key);
            }
            drop(state);
            inner.changed.notify_waiters();
        }
    }
}
struct Ticket {
    owner: Weak<Inner>,
    key: Key,
    id: u64,
    active: bool,
}
impl Ticket {
    fn remove(&mut self, state: &mut State) {
        if self.active {
            let group = state.groups.get_mut(&self.key).unwrap();
            if group
                .waiting
                .front()
                .is_some_and(|waiting| waiting.id == self.id)
            {
                group.waiting.pop_front();
            } else {
                group.waiting.retain(|waiting| waiting.id != self.id);
            }
            state.waiting -= 1;
            self.active = false;
        }
    }
}
impl Drop for Ticket {
    fn drop(&mut self) {
        if self.active {
            if let Some(inner) = self.owner.upgrade() {
                let mut state = inner
                    .state
                    .lock()
                    .unwrap_or_else(|error| error.into_inner());
                self.remove(&mut state);
                if state
                    .groups
                    .get(&self.key)
                    .is_some_and(|group| group.total == 0 && group.waiting.is_empty())
                {
                    state.groups.remove(&self.key);
                }
                drop(state);
                inner.changed.notify_waiters();
            }
        }
    }
}

pub(super) async fn checkout(
    config: &Config,
    node: usize,
    packet: Arc<[u8]>,
    selected: &Topology,
    deadline: Instant,
    ticket: &mut Option<Guard>,
) -> Result<Backend> {
    let key = Key::new(node, selected, packet.clone());
    loop {
        match config.pools.acquire(key.clone(), deadline).await? {
            Checkout::Idle(backend) => {
                if backend.healthy_idle() {
                    config.metrics.inc("pool_reuses_total", &[]);
                    return Ok(*backend);
                }
                config.metrics.inc("pool_discards_total", &[]);
                drop(backend);
            }
            Checkout::New(slot) => {
                let (mut backend, messages) =
                    connect(config, node, &packet, selected.generation, ticket).await?;
                backend.slot = Some(slot);
                backend.pool_owner = Some(config.pools.clone());
                let metadata: Arc<Messages> = Arc::new(
                    messages
                        .into_iter()
                        .filter(|(tag, _)| matches!(tag, b'R' | b'S' | b'Z'))
                        .collect(),
                );
                backend.startup_metadata = Some(metadata.clone());
                config.pools.publish(&key, metadata);
                return Ok(backend);
            }
        }
    }
}

impl std::fmt::Debug for Checkout {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Idle(_) => "Idle backend",
            Self::New(_) => "New backend slot",
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn topology() -> Topology {
        Topology {
            primary: Some(0),
            generation: 1,
            ..Topology::default()
        }
    }
    fn key(value: &[u8]) -> Key {
        Key::new(0, &topology(), Arc::from(value))
    }
    fn manager(size: usize, global: usize, reserve: usize, waiters: usize) -> Manager {
        Manager::new(
            Settings {
                mode: Mode::Session,
                size,
                global,
                reserve,
                waiters,
                reserve_delay: Duration::from_millis(500),
                idle: Duration::from_millis(100),
                lifetime: Duration::from_secs(1),
            },
            Metrics::default(),
        )
        .unwrap()
    }
    async fn queued(manager: &Manager, count: usize) {
        timeout(Duration::from_secs(1), async {
            while manager.snapshot().1 != count {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
    }
    async fn fake(manager: &Manager, slot: Slot) -> (Backend, TcpStream) {
        let (stream, peer) = crate::tests::socket_pair().await;
        (
            Backend {
                stream: BufReader::new(stream),
                _live: Metrics::default().guard(Event::Backend("node".into())),
                generation: 1,
                buffers: buffers::Pool::default(),
                cancel_identity: (1, 2),
                status: b'I',
                startup_metadata: Some(Arc::new(vec![(b'R', vec![0; 4]), (b'Z', vec![b'I'])])),
                idle_since: Instant::now(),
                slot: Some(slot),
                pool_owner: Some(manager.clone()),
            },
            peer,
        )
    }
    #[tokio::test]
    async fn fifo_reuse_and_weak_idle_ownership_do_not_leak_the_manager() {
        let manager = manager(1, 2, 0, 8);
        let key = key(b"identity");
        let Checkout::New(slot) = manager
            .acquire(key.clone(), Instant::now() + Duration::from_secs(2))
            .await
            .unwrap()
        else {
            panic!()
        };
        let (backend, mut peer) = fake(&manager, slot).await;
        let address = backend.stream.get_ref().local_addr().unwrap();
        let first_manager = manager.clone();
        let first_key = key.clone();
        let first = tokio::spawn(async move {
            first_manager
                .acquire(first_key, Instant::now() + Duration::from_secs(2))
                .await
        });
        queued(&manager, 1).await;
        let second_manager = manager.clone();
        let second_key = key.clone();
        let second = tokio::spawn(async move {
            second_manager
                .acquire(second_key, Instant::now() + Duration::from_secs(2))
                .await
        });
        queued(&manager, 2).await;
        assert!(manager.put(&key, backend, &topology()));
        let Checkout::Idle(backend) = first.await.unwrap().unwrap() else {
            panic!()
        };
        assert_eq!(backend.stream.get_ref().local_addr().unwrap(), address);
        assert!(!second.is_finished());
        assert!(manager.put(&key, *backend, &topology()));
        let Checkout::Idle(backend) = second.await.unwrap().unwrap() else {
            panic!()
        };
        assert!(manager.put(&key, *backend, &topology()));
        assert_eq!(manager.snapshot(), (1, 0));
        let weak = Arc::downgrade(manager.0.as_ref().unwrap());
        drop(manager);
        assert!(
            weak.upgrade().is_none(),
            "idle ownership formed a strong manager cycle"
        );
        assert_eq!(peer.read(&mut [0u8; 1]).await.unwrap(), 0);
    }
    #[tokio::test]
    async fn aborted_waiter_leaves_no_ticket_or_capacity_reservation() {
        let manager = manager(1, 1, 0, 1);
        let key = key(b"identity");
        let Checkout::New(slot) = manager
            .acquire(key.clone(), Instant::now() + Duration::from_secs(2))
            .await
            .unwrap()
        else {
            panic!()
        };
        let next_manager = manager.clone();
        let next_key = key.clone();
        let waiting = tokio::spawn(async move {
            next_manager
                .acquire(next_key, Instant::now() + Duration::from_secs(2))
                .await
        });
        queued(&manager, 1).await;
        waiting.abort();
        assert!(waiting.await.unwrap_err().is_cancelled());
        assert_eq!(manager.snapshot(), (1, 0));
        drop(slot);
        assert_eq!(manager.snapshot(), (0, 0));
        assert!(matches!(
            manager
                .acquire(key, Instant::now() + Duration::from_secs(2))
                .await
                .unwrap(),
            Checkout::New(_)
        ));
    }
    #[tokio::test(start_paused = true)]
    async fn reserve_delay_and_independent_identity_progress_use_real_admission_state() {
        let manager = manager(1, 3, 1, 8);
        let a = key(b"a");
        let b = key(b"b");
        let first = manager
            .acquire(a.clone(), Instant::now() + Duration::from_secs(2))
            .await
            .unwrap();
        let next_manager = manager.clone();
        let waiting = tokio::spawn(async move {
            next_manager
                .acquire(a, Instant::now() + Duration::from_secs(2))
                .await
        });
        queued(&manager, 1).await;
        let other = manager
            .acquire(b, Instant::now() + Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(manager.snapshot(), (2, 1));
        tokio::time::advance(Duration::from_millis(499)).await;
        assert!(!waiting.is_finished());
        tokio::time::advance(Duration::from_millis(1)).await;
        let reserve = waiting.await.unwrap().unwrap();
        assert_eq!(manager.snapshot(), (3, 0));
        drop((first, other, reserve));
        assert_eq!(manager.snapshot(), (0, 0));
    }
    #[tokio::test(start_paused = true)]
    async fn full_wait_queue_and_expired_deadline_are_terminal_and_refund_tickets() {
        let manager = manager(1, 1, 0, 1);
        let a = key(b"a");
        let owned = manager
            .acquire(a.clone(), Instant::now() + Duration::from_secs(2))
            .await
            .unwrap();
        let next_manager = manager.clone();
        let next_key = a.clone();
        let waiting = tokio::spawn(async move {
            next_manager
                .acquire(next_key, Instant::now() + Duration::from_millis(50))
                .await
        });
        queued(&manager, 1).await;
        assert!(manager
            .acquire(key(b"b"), Instant::now() + Duration::from_secs(2))
            .await
            .unwrap_err()
            .is::<Rejected>());
        tokio::time::advance(Duration::from_millis(50)).await;
        assert!(waiting.await.unwrap().unwrap_err().is::<Rejected>());
        assert_eq!(manager.snapshot(), (1, 0));
        drop(owned);
        assert_eq!(manager.snapshot(), (0, 0));
    }
    #[tokio::test]
    async fn generation_retirement_and_unexpected_idle_bytes_discard_ownership() {
        let manager = manager(1, 2, 0, 2);
        let key = key(b"identity");
        let Checkout::New(slot) = manager
            .acquire(key.clone(), Instant::now() + Duration::from_secs(2))
            .await
            .unwrap()
        else {
            panic!()
        };
        let (backend, mut peer) = fake(&manager, slot).await;
        assert!(manager.put(&key, backend, &topology()));
        let mut promoted = topology();
        promoted.generation += 1;
        manager.maintain(&promoted);
        assert_eq!(manager.snapshot(), (0, 0));
        assert_eq!(peer.read(&mut [0u8; 1]).await.unwrap(), 0);
        let Checkout::New(slot) = manager
            .acquire(key.clone(), Instant::now() + Duration::from_secs(2))
            .await
            .unwrap()
        else {
            panic!()
        };
        let (backend, mut peer) = fake(&manager, slot).await;
        peer.write_all(b"unexpected").await.unwrap();
        backend.stream.get_ref().readable().await.unwrap();
        assert!(!backend.healthy_idle());
        drop(backend);
        assert_eq!(manager.snapshot(), (0, 0));
    }
}
