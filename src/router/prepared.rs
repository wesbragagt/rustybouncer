use super::*;
use tokio::sync::OwnedSemaphorePermit;

#[derive(Debug)]
pub(super) struct Exhausted;
impl std::fmt::Display for Exhausted {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("logical prepared capacity exhausted")
    }
}
impl std::error::Error for Exhausted {}

#[derive(Debug)]
pub(super) struct Malformed;
impl std::fmt::Display for Malformed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("malformed extended message")
    }
}
impl std::error::Error for Malformed {}

struct Cursor<'a> {
    bytes: &'a [u8],
    offset: usize,
}
impl<'a> Cursor<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, offset: 0 }
    }
    fn take(&mut self, count: usize) -> Result<&'a [u8]> {
        let end = self
            .offset
            .checked_add(count)
            .context("protocol offset overflow")?;
        let data = self
            .bytes
            .get(self.offset..end)
            .context("truncated extended message")?;
        self.offset = end;
        Ok(data)
    }
    fn name(&mut self) -> Result<&'a [u8]> {
        let rest = &self.bytes[self.offset..];
        let count = rest
            .iter()
            .position(|byte| *byte == 0)
            .context("unterminated protocol string")?;
        let name = self.take(count)?;
        self.take(1)?;
        Ok(name)
    }
    fn u16(&mut self) -> Result<usize> {
        Ok(u16::from_be_bytes(self.take(2)?.try_into()?) as usize)
    }
    fn int(&mut self) -> Result<i32> {
        Ok(i32::from_be_bytes(self.take(4)?.try_into()?))
    }
    fn bind(&mut self) -> Result<Message<'a>> {
        let portal = self.name()?;
        let statement = self.name()?;
        let formats = self.u16()?;
        self.take(formats * 2)?;
        let parameters = self.u16()?;
        for _ in 0..parameters {
            let length = self.int()?;
            if length < -1 {
                bail!("invalid parameter length");
            }
            if length >= 0 {
                self.take(length as usize)?;
            }
        }
        let results = self.u16()?;
        self.take(results * 2)?;
        Ok(Message::Bind { portal, statement })
    }
    fn end(self) -> Result<()> {
        if self.offset != self.bytes.len() {
            bail!("trailing extended message bytes");
        }
        Ok(())
    }
}

pub(super) enum Message<'a> {
    Parse {
        name: &'a [u8],
        sql: &'a [u8],
        types_offset: usize,
    },
    Bind {
        portal: &'a [u8],
        statement: &'a [u8],
    },
    Describe {
        kind: u8,
        name: &'a [u8],
    },
    Execute {
        portal: &'a [u8],
    },
    Close {
        kind: u8,
        name: &'a [u8],
    },
    Flush,
    Sync,
}
impl<'a> Message<'a> {
    pub fn parse(tag: u8, body: &'a [u8]) -> Result<Self> {
        Self::decode(tag, body).map_err(|error| error.context(Malformed))
    }
    fn decode(tag: u8, body: &'a [u8]) -> Result<Self> {
        let mut cursor = Cursor::new(body);
        let result = match tag {
            b'P' => {
                let name = cursor.name()?;
                let sql = cursor.name()?;
                let types_offset = cursor.offset;
                let count = cursor.u16()?;
                cursor.take(count * 4)?;
                Self::Parse {
                    name,
                    sql,
                    types_offset,
                }
            }
            b'B' => cursor.bind()?,
            b'D' | b'C' => {
                let kind = cursor.take(1)?[0];
                if !matches!(kind, b'S' | b'P') {
                    bail!("invalid describe/close target");
                }
                let name = cursor.name()?;
                if tag == b'D' {
                    Self::Describe { kind, name }
                } else {
                    Self::Close { kind, name }
                }
            }
            b'E' => {
                let portal = cursor.name()?;
                cursor.int()?;
                Self::Execute { portal }
            }
            b'H' => Self::Flush,
            b'S' => Self::Sync,
            _ => bail!("unsupported extended message"),
        };
        cursor.end()?;
        Ok(result)
    }
}

pub(super) struct Definition {
    pub id: u64,
    body: Box<[u8]>,
    name_end: usize,
    sql_end: usize,
    types_offset: usize,
    pub shape: Box<[u8]>,
    pub resolved: Box<[u8]>, // encoded Int16 count + original inferred OIDs
    _bytes: OwnedSemaphorePermit,
}
impl Definition {
    pub fn name(&self) -> &[u8] {
        &self.body[..self.name_end]
    }
    pub fn sql(&self) -> &[u8] {
        &self.body[self.name_end + 1..self.sql_end]
    }
    pub fn prefix(&self) -> &[u8] {
        &self.body[..self.types_offset]
    }
    pub fn requires_session(&self) -> bool {
        session_required(self.sql())
    }
}

pub(super) fn session_required(sql: &[u8]) -> bool {
    match std::str::from_utf8(sql) {
        Ok(sql) => affinity::session_required(sql, classify(sql)),
        Err(_) => true,
    }
}

struct Portal {
    definition: Option<Arc<Definition>>,
    _bytes: OwnedSemaphorePermit,
}
pub(super) struct Cache {
    definitions: HashMap<Vec<u8>, Arc<Definition>>,
    portals: HashMap<Vec<u8>, Portal>,
    bytes: Arc<Semaphore>,
    limit: usize,
    next: u64,
    native: bool,
}
impl Cache {
    pub fn new(entries: usize, bytes: usize, native: bool) -> Self {
        Self {
            definitions: HashMap::new(),
            portals: HashMap::new(),
            bytes: Arc::new(Semaphore::new(bytes)),
            limit: entries,
            next: 1,
            native,
        }
    }
    fn reserve(&self, bytes: usize) -> Result<OwnedSemaphorePermit> {
        Ok(self
            .bytes
            .clone()
            .try_acquire_many_owned(u32::try_from(bytes.max(1))?)
            .map_err(|_| Exhausted)?)
    }
    pub fn get(&self, name: &[u8]) -> Option<Arc<Definition>> {
        self.definitions.get(name).cloned()
    }
    pub fn all(&self) -> Vec<Arc<Definition>> {
        self.definitions.values().cloned().collect()
    }
    pub fn has_named(&self) -> bool {
        self.definitions.keys().any(|n| !n.is_empty())
    }
    pub fn native(&self) -> bool {
        self.native
    }
    pub fn pin_native(&mut self) {
        self.native = true;
        self.definitions.clear();
        self.portals.clear();
    }
    pub fn unnamed_query(&mut self) {
        self.definitions.remove(&b""[..]);
        self.portals.remove(&b""[..]);
    }
    pub fn idle(&mut self) {
        self.portals.clear();
    }
    pub fn parse_started(&mut self, name: &[u8]) {
        if name.is_empty() {
            self.definitions.remove(name);
        }
    }
    pub fn insert(
        &mut self,
        body: Vec<u8>,
        types: Vec<u32>,
        shape: Vec<u8>,
    ) -> Result<Arc<Definition>> {
        let Message::Parse {
            name,
            sql,
            types_offset,
        } = Message::parse(b'P', &body)?
        else {
            unreachable!()
        };
        if self.definitions.len() >= self.limit && !self.definitions.contains_key(name) {
            return Err(Exhausted.into());
        }
        let name_end = name.len();
        let sql_end = name_end + 1 + sql.len();
        let charge = body
            .len()
            .checked_add(2 + types.len() * 4)
            .and_then(|size| size.checked_add(name.len()))
            .and_then(|size| size.checked_add(shape.len()))
            .context("cache size overflow")?;
        let bytes = self.reserve(charge)?;
        let id = self.next;
        self.next = self
            .next
            .checked_add(1)
            .context("prepared identity exhausted")?;
        let mut resolved = Vec::new();
        resolved.try_reserve_exact(2 + types.len() * 4)?;
        resolved.extend(u16::try_from(types.len())?.to_be_bytes());
        for oid in types {
            resolved.extend(oid.to_be_bytes());
        }
        let definition = Arc::new(Definition {
            id,
            body: body.into_boxed_slice(),
            name_end,
            sql_end,
            types_offset,
            resolved: resolved.into_boxed_slice(),
            shape: shape.into_boxed_slice(),
            _bytes: bytes,
        });
        self.definitions
            .insert(definition.name().to_vec(), definition.clone());
        Ok(definition)
    }
    pub fn bind_started(&mut self, portal: &[u8]) {
        if portal.is_empty() {
            self.portals.remove(portal);
        }
    }
    pub fn bound(&mut self, portal: &[u8], statement: &[u8]) -> Result<()> {
        if self.native {
            return Ok(());
        }
        if self.portals.len() >= self.limit && !self.portals.contains_key(portal) {
            return Err(Exhausted.into());
        }
        let bytes = self.reserve(portal.len())?;
        self.portals.insert(
            portal.to_vec(),
            Portal {
                definition: self.get(statement),
                _bytes: bytes,
            },
        );
        Ok(())
    }
    pub fn executed_session_effect(&self, portal: &[u8]) -> bool {
        self.portals
            .get(portal)
            .and_then(|portal| portal.definition.as_ref())
            .is_none_or(|definition| definition.requires_session())
    }
    pub fn close(&mut self, kind: u8, name: &[u8]) {
        if kind == b'P' {
            self.portals.remove(name);
        } else {
            // Bound portals own plan references independently of statement names.
            self.definitions.remove(name);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn parse_body(name: &[u8], sql: &[u8]) -> Vec<u8> {
        [name, b"\0", sql, b"\0\0\x01\0\0\0\0"].concat()
    }
    #[test]
    fn retained_original_parse_and_resolved_types_have_independent_charge_ownership() {
        let mut cache = Cache::new(2, 200, false);
        let body = parse_body(b"s", b"SELECT $1::int");
        let original = body.clone();
        let definition = cache.insert(body, vec![23], vec![b'n']).unwrap();
        assert_eq!(definition.body.as_ref(), original);
        assert_eq!(definition.resolved.as_ref(), &[0, 1, 0, 0, 0, 23]);
        assert_eq!(definition.sql(), b"SELECT $1::int");
        assert_eq!(definition.prefix(), b"s\0SELECT $1::int\0");
        cache.bound(b"portal", b"s").unwrap();
        let charged = cache.bytes.available_permits();
        cache.definitions.clear();
        drop(definition);
        assert_eq!(cache.bytes.available_permits(), charged);
        cache.idle();
        assert_eq!(cache.bytes.available_permits(), 200);
    }
    #[test]
    fn replacing_unnamed_retains_portal_definition_until_its_transaction_ends() {
        let mut cache = Cache::new(1, 200, false);
        let first = cache
            .insert(parse_body(b"", b"SELECT $1::int"), vec![23], vec![b'n'])
            .unwrap();
        cache.bound(b"old", b"").unwrap();
        cache.parse_started(b"");
        let second = cache
            .insert(parse_body(b"", b"SELECT $1::text"), vec![25], vec![b'n'])
            .unwrap();
        assert_ne!(first.id, second.id);
        assert_eq!(
            cache.portals[b"old".as_slice()]
                .definition
                .as_ref()
                .unwrap()
                .id,
            first.id
        );
        drop(first);
        drop(second);
        cache.idle();
        cache.definitions.clear();
        assert_eq!(cache.bytes.available_permits(), 200);
    }
    #[test]
    fn malformed_extended_lengths_targets_nulls_and_trailing_bytes_are_rejected() {
        for (tag, body) in [
            (b'P', &b"s\0SELECT 1\0\0\x01\0"[..]),
            (b'B', &b"\0\0\0\0\0\x01\xff\xff\xff\xfe\0\0"[..]),
            (b'D', &b"Xname\0"[..]),
            (b'S', &b"x"[..]),
            (b'E', &b"\0\0\0"[..]),
        ] {
            assert!(Message::parse(tag, body).is_err());
        }
        assert!(Message::parse(b'B', b"\0\0\0\0\0\x01\xff\xff\xff\xff\0\0").is_ok());
        let mut cache = Cache::new(1, 30, false);
        assert!(cache
            .insert(parse_body(b"large", &[b'x'; 32]), vec![23], vec![b'n'])
            .err()
            .unwrap()
            .is::<Exhausted>());
        assert_eq!(cache.bytes.available_permits(), 30);
    }
}
