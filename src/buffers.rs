//! Process-owned reusable bulk storage. Free slabs retain their capacity charge.
use crate::metrics::Metrics;
use anyhow::{bail, Context, Result};
use std::{
    fmt,
    sync::{Arc, Mutex},
};

pub const SLAB_BYTES: usize = 64 * 1024;
pub const SMALL_BYTES: usize = 256;
pub const DEFAULT_LIMIT: usize = 64 * 1024 * 1024;
#[derive(Debug)]
pub struct Exhausted(&'static str);
impl fmt::Display for Exhausted {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for Exhausted {}

struct State {
    highwater: usize,
    slots: Vec<Option<Vec<u8>>>,
    free: Vec<usize>,
    allocated: usize,
}
struct Inner {
    state: Mutex<State>,
    metrics: Metrics,
    limit: usize,
}
#[derive(Clone)]
pub struct Pool(Arc<Inner>);
impl Default for Pool {
    fn default() -> Self {
        Self::new(DEFAULT_LIMIT, Metrics::default()).unwrap()
    }
}
impl Pool {
    pub fn new(limit: usize, metrics: Metrics) -> Result<Self> {
        if limit < SLAB_BYTES || !limit.is_multiple_of(SLAB_BYTES) || limit > i64::MAX as usize {
            bail!("result buffer limit must be a positive multiple of 64 KiB and fit i64");
        }
        let count = limit / SLAB_BYTES;
        let mut slots = Vec::new();
        let mut free = Vec::new();
        slots
            .try_reserve_exact(count)
            .context("result buffer slot metadata")?;
        free.try_reserve_exact(count)
            .context("result buffer free metadata")?;
        slots.resize_with(count, || None);
        free.extend((0..count).rev());
        metrics.set("result_buffer_limit_bytes", &[], limit as i64);
        metrics.set("result_buffer_reserved_bytes", &[], 0);
        metrics.set("result_buffer_allocated_bytes", &[], 0);
        Ok(Self(Arc::new(Inner {
            state: Mutex::new(State {
                slots,
                free,
                allocated: 0,
                highwater: 0,
            }),
            metrics,
            limit,
        })))
    }
    pub fn try_get(&self) -> Result<Slab> {
        let (index, cached) = {
            let mut state = self.0.state.lock().unwrap();
            let Some(index) = state.free.pop() else {
                self.0.metrics.inc("result_buffer_rejections_total", &[]);
                return Err(Exhausted("global result buffer capacity exhausted").into());
            };
            let cached = state.slots[index].take();
            state.highwater = state
                .highwater
                .max((state.slots.len() - state.free.len()) * SLAB_BYTES);
            self.0
                .metrics
                .set("result_buffer_highwater_bytes", &[], state.highwater as i64);
            self.0.metrics.set(
                "result_buffer_reserved_bytes",
                &[],
                ((state.slots.len() - state.free.len()) * SLAB_BYTES) as i64,
            );
            (index, cached)
        };
        let mut slab = Slab {
            pool: self.clone(),
            index,
            bytes: cached.unwrap_or_default(),
        };
        if slab.bytes.capacity() == 0 {
            slab.bytes
                .try_reserve_exact(SLAB_BYTES)
                .map_err(|_| Exhausted("result buffer allocation failed"))?;
            let mut state = self.0.state.lock().unwrap();
            state.allocated += SLAB_BYTES;
            debug_assert!(state.allocated <= self.0.limit);
            self.0
                .metrics
                .set("result_buffer_allocated_bytes", &[], state.allocated as i64);
        }
        debug_assert_eq!(slab.bytes.capacity(), SLAB_BYTES);
        slab.bytes.clear();
        Ok(slab)
    }
    #[cfg(test)]
    pub fn snapshot(&self) -> (usize, usize) {
        let state = self.0.state.lock().unwrap();
        (
            (state.slots.len() - state.free.len()) * SLAB_BYTES,
            state.allocated,
        )
    }
}

pub struct Slab {
    pool: Pool,
    index: usize,
    bytes: Vec<u8>,
}
impl fmt::Debug for Slab {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Slab")
            .field("length", &self.bytes.len())
            .finish()
    }
}
impl Slab {
    pub fn remaining(&self) -> usize {
        SLAB_BYTES - self.bytes.len()
    }
    pub fn data(&self) -> &[u8] {
        &self.bytes
    }
    pub fn reserve(&mut self, wanted: usize) -> &mut [u8] {
        let offset = self.bytes.len();
        let count = wanted.min(self.remaining());
        self.bytes.resize(offset + count, 0);
        &mut self.bytes[offset..offset + count]
    }
}
impl Drop for Slab {
    fn drop(&mut self) {
        let mut state = self
            .pool
            .0
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        debug_assert!(state.slots[self.index].is_none());
        let mut bytes = std::mem::take(&mut self.bytes);
        bytes.clear();
        if bytes.capacity() != 0 {
            state.slots[self.index] = Some(bytes);
        }
        state.free.push(self.index); // Capacity was provisioned before startup.
        self.pool.0.metrics.set(
            "result_buffer_reserved_bytes",
            &[],
            ((state.slots.len() - state.free.len()) * SLAB_BYTES) as i64,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn saturation_refunds_live_ownership_and_reuses_charged_capacity() {
        let pool = Pool::new(2 * SLAB_BYTES, Metrics::default()).unwrap();
        let a = pool.try_get().unwrap();
        let b = pool.try_get().unwrap();
        assert!(pool.try_get().unwrap_err().is::<Exhausted>());
        assert_eq!(pool.snapshot(), (2 * SLAB_BYTES, 2 * SLAB_BYTES));
        drop(a);
        drop(b);
        assert_eq!(pool.snapshot(), (0, 2 * SLAB_BYTES));
        for _ in 0..100 {
            drop(pool.try_get().unwrap());
        }
        assert_eq!(pool.snapshot(), (0, 2 * SLAB_BYTES));
    }
    #[test]
    fn reused_bytes_are_private_until_initialized_for_the_next_owner() {
        let pool = Pool::new(SLAB_BYTES, Metrics::default()).unwrap();
        let mut a = pool.try_get().unwrap();
        a.reserve(16).fill(255);
        drop(a);
        let mut b = pool.try_get().unwrap();
        assert!(b.data().is_empty());
        assert_eq!(b.reserve(16), &[0u8; 16]);
        assert_eq!(b.remaining(), SLAB_BYTES - 16);
    }
    #[tokio::test]
    async fn aborted_future_returns_every_slab_without_detaching_cleanup() {
        let pool = Pool::new(SLAB_BYTES, Metrics::default()).unwrap();
        let lease = pool.try_get().unwrap();
        let task = tokio::spawn(async move {
            let _lease = lease;
            std::future::pending::<()>().await;
        });
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        assert_eq!(pool.snapshot(), (0, SLAB_BYTES));
        assert!(pool.try_get().is_ok());
    }
    #[test]
    fn invalid_budget_is_rejected_before_queries() {
        for limit in [0, 1, SLAB_BYTES - 1, SLAB_BYTES + 1] {
            assert!(Pool::new(limit, Metrics::default()).is_err());
        }
    }
}
