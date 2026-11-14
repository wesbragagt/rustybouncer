//! SCRAM verifier lookup and proof exchange. No plaintext password or verifier cache.
use crate::*;
use base64::{engine::general_purpose::STANDARD, Engine};
use hmac::{Hmac, KeyInit, Mac};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Query {
    pub user: String,
    pub query: String,
    pub database: Option<String>,
}
#[derive(Clone)]
pub struct Keys {
    client: [u8; 32],
    server: [u8; 32],
}
struct Verifier {
    iterations: u32,
    salt: String,
    stored: [u8; 32],
    server: [u8; 32],
}
fn decode(value: &str) -> Result<[u8; 32]> {
    STANDARD
        .decode(value)?
        .try_into()
        .map_err(|_| anyhow::anyhow!("invalid SCRAM key length"))
}
fn mac(key: &[u8], value: &[u8]) -> [u8; 32] {
    let mut hmac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC accepts any key length");
    hmac.update(value);
    hmac.finalize().into_bytes().into()
}
fn xor(a: &[u8; 32], b: &[u8; 32]) -> [u8; 32] {
    std::array::from_fn(|i| a[i] ^ b[i])
}
fn nonce() -> Result<String> {
    let mut bytes = [0u8; 24];
    getrandom::fill(&mut bytes)?;
    Ok(STANDARD.encode(bytes))
}
fn attribute<'a>(message: &'a str, name: &str) -> Result<&'a str> {
    let mut values = message
        .split(',')
        .filter_map(|field| field.strip_prefix(name));
    let value = values.next().context("missing SCRAM attribute")?;
    if value.is_empty() || values.next().is_some() {
        bail!("invalid SCRAM attribute")
    }
    Ok(value)
}
impl Verifier {
    fn parse(value: &str) -> Result<Self> {
        let value = value
            .strip_prefix("SCRAM-SHA-256$")
            .context("auth_query must return a SCRAM-SHA-256 verifier")?;
        let (params, keys) = value.split_once('$').context("invalid SCRAM verifier")?;
        let (iterations, salt) = params.split_once(':').context("invalid SCRAM parameters")?;
        let iterations = iterations.parse()?;
        if !(1..=100_000).contains(&iterations) || STANDARD.decode(salt)?.is_empty() {
            bail!("invalid SCRAM parameters")
        }
        let (stored, server) = keys.split_once(':').context("invalid SCRAM keys")?;
        Ok(Self {
            iterations,
            salt: salt.into(),
            stored: decode(stored)?,
            server: decode(server)?,
        })
    }
}
pub fn identity(packet: &[u8]) -> Result<(String, String)> {
    let fields = packet.get(8..).context("invalid startup")?;
    let mut fields = fields.split(|b| *b == 0);
    let mut user = None;
    let mut database = None;
    loop {
        let key = fields.next().context("unterminated startup")?;
        if key.is_empty() {
            break;
        }
        let value = fields.next().context("missing startup value")?;
        let value = std::str::from_utf8(value)?.to_owned();
        match key {
            b"user" if user.is_none() => user = Some(value),
            b"database" if database.is_none() => database = Some(value),
            b"user" | b"database" => bail!("duplicate startup identity"),
            _ => (),
        }
    }
    if fields.any(|value| !value.is_empty()) {
        bail!("trailing startup fields")
    }
    let user = user
        .filter(|value| !value.is_empty())
        .context("missing startup user")?;
    let database = database
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| user.clone());
    Ok((user, database))
}
// The driver is owned even if a lookup/probe is cancelled by its deadline.
pub struct Driver(tokio::task::JoinHandle<()>);
impl Drop for Driver {
    fn drop(&mut self) {
        self.0.abort();
    }
}
pub async fn database(
    config: &Config,
    node: usize,
    user: &str,
    db: &str,
    password: Option<&str>,
) -> Result<(tokio_postgres::Client, Driver)> {
    let node = &config.nodes[node];
    let stream = TcpStream::connect((node.host.as_str(), node.port)).await?;
    let stream = transport::backend(
        stream,
        node.tls_server_name.as_deref().unwrap_or(&node.host),
        config.backend_tls.as_ref(),
    )
    .await?;
    let mut pg = tokio_postgres::Config::new();
    pg.user(user).dbname(db);
    if let Some(password) = password {
        pg.password(password);
    }
    let (client, connection) = pg.connect_raw(stream, NoTls).await?;
    let driver = Driver(tokio::spawn(async move {
        let _ = connection.await;
    }));
    Ok((client, driver))
}
async fn lookup(
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    user: &str,
    db: &str,
) -> Result<Verifier> {
    let query = config.auth_query.as_ref().context("missing auth_query")?;
    let node = loop {
        if let Some(node) = topology.borrow_and_update().primary {
            break node;
        }
        topology.changed().await?;
    };
    let (client, _driver) = database(
        config,
        node,
        &query.user,
        query.database.as_deref().unwrap_or(db),
        None,
    )
    .await?;
    let rows = client.query(&query.query, &[&user]).await?;
    if rows.len() != 1 || rows[0].len() != 2 {
        bail!("auth_query must return exactly one username/verifier row")
    }
    let name: Option<&str> = rows[0].try_get(0)?;
    let secret: Option<&str> = rows[0].try_get(1)?;
    if name != Some(user) {
        bail!("auth_query identity mismatch")
    }
    Verifier::parse(secret.context("role has no valid password")?)
}
async fn password_frame(stream: &mut transport::Stream) -> Result<Vec<u8>> {
    let tag = stream.read_u8().await?;
    let length = stream.read_u32().await? as usize;
    if tag != b'p' || !(4..=4100).contains(&length) {
        bail!("invalid SCRAM response")
    }
    let mut body = vec![0; length - 4];
    stream.read_exact(&mut body).await?;
    Ok(body)
}
async fn challenge(stream: &mut transport::Stream, code: u32, value: &[u8]) -> Result<()> {
    let mut body = code.to_be_bytes().to_vec();
    body.extend_from_slice(value);
    router::wire::send(stream, b'R', &body).await
}
async fn exchange(stream: &mut transport::Stream, verifier: Verifier) -> Result<Keys> {
    challenge(stream, 10, b"SCRAM-SHA-256\0\0").await?;
    let first = password_frame(stream).await?;
    let prefix = b"SCRAM-SHA-256\0";
    if !first.starts_with(prefix) || first.len() < prefix.len() + 4 {
        bail!("unsupported SCRAM mechanism")
    }
    let length = u32::from_be_bytes(first[prefix.len()..prefix.len() + 4].try_into()?) as usize;
    let first = &first[prefix.len() + 4..];
    if first.len() != length {
        bail!("invalid SCRAM initial response length")
    }
    let first = std::str::from_utf8(first)?;
    let header = first.get(..3).context("missing SCRAM GS2 header")?;
    if !matches!(header, "n,," | "y,,") {
        bail!("unsupported SCRAM channel binding")
    }
    let bare = &first[3..];
    if !bare.starts_with("n=") || bare.split(',').count() != 2 {
        bail!("invalid SCRAM client first")
    }
    let client_nonce = attribute(bare, "r=")?;
    if client_nonce.len() < 16 || client_nonce.bytes().any(|b| !(0x21..=0x7e).contains(&b)) {
        bail!("invalid SCRAM nonce")
    }
    let combined = format!("{client_nonce}{}", nonce()?);
    let server_first = format!("r={combined},s={},i={}", verifier.salt, verifier.iterations);
    challenge(stream, 11, server_first.as_bytes()).await?;
    let last = password_frame(stream).await?;
    let last = std::str::from_utf8(&last)?;
    if attribute(last, "c=")? != STANDARD.encode(header)
        || attribute(last, "r=")? != combined
        || last.split(',').count() != 3
    {
        bail!("invalid SCRAM client final")
    }
    let (without_proof, proof) = last.rsplit_once(",p=").context("missing SCRAM proof")?;
    let auth = format!("{bare},{server_first},{without_proof}");
    let client = xor(&decode(proof)?, &mac(&verifier.stored, auth.as_bytes()));
    let stored: [u8; 32] = Sha256::digest(client).into();
    if !bool::from(stored.ct_eq(&verifier.stored)) {
        bail!("invalid SCRAM proof")
    }
    let signature = STANDARD.encode(mac(&verifier.server, auth.as_bytes()));
    challenge(stream, 12, format!("v={signature}").as_bytes()).await?;
    Ok(Keys {
        client,
        server: verifier.server,
    })
}
pub async fn authenticate(
    stream: &mut transport::Stream,
    config: &Config,
    topology: &mut watch::Receiver<Topology>,
    packet: &[u8],
) -> Result<Keys> {
    if !stream.encrypted() {
        bail!("SCRAM authentication requires TLS")
    }
    let (user, db) = identity(packet)?;
    let result = timeout(Duration::from_millis(config.startup_timeout_ms), async {
        let verifier = lookup(config, topology, &user, &db).await?;
        exchange(stream, verifier).await
    })
    .await;
    match result {
        Ok(Ok(keys)) => Ok(keys),
        _ => {
            pg_error(
                stream,
                "28P01",
                "rustybouncer: password authentication failed",
            )
            .await?;
            bail!("client authentication failed")
        }
    }
}
// Backend SCRAM uses the recovered ClientKey and verifier ServerKey. This is
// possible only when auth_query and the backend refer to the same role password.
pub struct BackendAuth {
    keys: Keys,
    first: String,
    auth: Option<String>,
    phase: u8,
}
impl BackendAuth {
    pub fn new(keys: Keys) -> Result<Self> {
        Ok(Self {
            keys,
            first: format!("n=,r={}", nonce()?),
            auth: None,
            phase: 0,
        })
    }
    pub async fn respond(&mut self, stream: &mut transport::Stream, body: &[u8]) -> Result<()> {
        let code = u32::from_be_bytes(
            body.get(..4)
                .context("invalid authentication")?
                .try_into()?,
        );
        match code {
            10 if self.phase == 0 => {
                if !body[4..].split(|b| *b == 0).any(|m| m == b"SCRAM-SHA-256") {
                    bail!("backend does not offer SCRAM-SHA-256")
                }
                let initial = format!("n,,{}", self.first);
                let mut response = b"SCRAM-SHA-256\0".to_vec();
                response.extend_from_slice(&(initial.len() as u32).to_be_bytes());
                response.extend_from_slice(initial.as_bytes());
                router::wire::send(stream, b'p', &response).await?;
                self.phase = 1;
            }
            11 if self.phase == 1 => {
                let message = std::str::from_utf8(&body[4..])?;
                let combined = attribute(message, "r=")?;
                let original = attribute(&self.first, "r=")?;
                if !combined.starts_with(original) || combined.len() <= original.len() {
                    bail!("backend SCRAM nonce mismatch")
                }
                let last = format!("c=biws,r={combined}");
                let auth = format!("{},{message},{last}", self.first);
                let stored: [u8; 32] = Sha256::digest(self.keys.client).into();
                let proof = STANDARD.encode(xor(&self.keys.client, &mac(&stored, auth.as_bytes())));
                router::wire::send(stream, b'p', format!("{last},p={proof}").as_bytes()).await?;
                self.auth = Some(auth);
                self.phase = 2;
            }
            12 if self.phase == 2 => {
                let auth = self.auth.as_ref().context("unexpected SCRAM final")?;
                let signature = decode(attribute(std::str::from_utf8(&body[4..])?, "v=")?)?;
                if !bool::from(signature.ct_eq(&mac(&self.keys.server, auth.as_bytes()))) {
                    bail!("invalid backend SCRAM signature")
                }
                self.phase = 3;
            }
            _ => bail!("unsupported backend authentication exchange"),
        }
        Ok(())
    }
    pub fn verified(&self) -> bool {
        self.phase == 3
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn packet(fields: &[u8]) -> Vec<u8> {
        let mut packet = vec![0; 8];
        packet.extend_from_slice(fields);
        packet
    }
    #[test]
    fn startup_identity_rejects_duplicates_and_missing_terminators() {
        assert_eq!(
            identity(&packet(b"user\0app\0\0")).unwrap(),
            ("app".into(), "app".into())
        );
        for fields in [
            &b"user\0app\0user\0admin\0\0"[..],
            b"user\0app\0database\0a\0database\0b\0\0",
            b"user\0app",
            b"user\0\0\0",
            b"user\0app\0\0extra",
        ] {
            assert!(identity(&packet(fields)).is_err());
        }
    }
    #[test]
    fn verifier_rejects_unsupported_hashes_invalid_keys_and_excessive_iterations() {
        let key = STANDARD.encode([7; 32]);
        let valid = format!("SCRAM-SHA-256$4096:c2FsdA==${key}:{key}");
        assert!(Verifier::parse(&valid).is_ok());
        for value in [
            valid.replace("4096", "0"),
            valid.replace("4096", "100001"),
            valid.replace("c2FsdA==", ""),
            valid.replace(&key, "eA=="),
            "md5abcd".into(),
        ] {
            assert!(Verifier::parse(&value).is_err());
        }
        assert!(attribute("r=one,r=two", "r=").is_err());
        assert!(attribute("r=", "r=").is_err());
    }
    async fn server_message(stream: &mut transport::Stream) -> Vec<u8> {
        assert_eq!(stream.read_u8().await.unwrap(), b'R');
        let length = stream.read_u32().await.unwrap() as usize;
        let mut body = vec![0; length - 4];
        stream.read_exact(&mut body).await.unwrap();
        body
    }
    #[tokio::test]
    async fn independent_postgres_scram_client_validates_both_gs2_headers_and_wrong_password() {
        use postgres_protocol::authentication::sasl::{ChannelBinding, ScramSha256};
        for (binding, password, valid) in [
            (ChannelBinding::unsupported(), &b"pencil"[..], true),
            (ChannelBinding::unrequested(), &b"pencil"[..], true),
            (ChannelBinding::unrequested(), &b"wrong"[..], false),
        ] {
            // PBKDF2 is used only to construct an independent fixture verifier;
            // the production server verifies proofs without deriving passwords.
            let mut block = mac(b"pencil", b"salt\0\0\0\x01");
            let mut salted = block;
            for _ in 1..4096 {
                block = mac(b"pencil", &block);
                salted = xor(&salted, &block);
            }
            let client_key = mac(&salted, b"Client Key");
            let stored: [u8; 32] = Sha256::digest(client_key).into();
            let server_key = mac(&salted, b"Server Key");
            let verifier = Verifier::parse(&format!(
                "SCRAM-SHA-256$4096:c2FsdA==${}:{}",
                STANDARD.encode(stored),
                STANDARD.encode(server_key)
            ))
            .unwrap();
            let (client, server) = crate::tests::socket_pair().await;
            let task = tokio::spawn(async move { exchange(&mut server.into(), verifier).await });
            let mut client = transport::Stream::from(client);
            assert_eq!(
                &server_message(&mut client).await[..4],
                &10u32.to_be_bytes()
            );
            let mut scram = ScramSha256::new(password, binding);
            let mut first = b"SCRAM-SHA-256\0".to_vec();
            first.extend_from_slice(&(scram.message().len() as u32).to_be_bytes());
            first.extend_from_slice(scram.message());
            router::wire::send(&mut client, b'p', &first).await.unwrap();
            let continuation = server_message(&mut client).await;
            assert_eq!(&continuation[..4], &11u32.to_be_bytes());
            scram.update(&continuation[4..]).unwrap();
            router::wire::send(&mut client, b'p', scram.message())
                .await
                .unwrap();
            let result = task.await.unwrap();
            if valid {
                let keys = result.unwrap();
                assert_eq!(keys.client, client_key);
                assert_eq!(keys.server, server_key);
                let final_message = server_message(&mut client).await;
                assert_eq!(&final_message[..4], &12u32.to_be_bytes());
                scram.finish(&final_message[4..]).unwrap();
            } else {
                assert!(result.is_err());
            }
        }
    }
    #[tokio::test]
    async fn oversized_auth_frame_is_rejected_before_reading_body() {
        let (mut peer, stream) = crate::tests::socket_pair().await;
        peer.write_all(&[b'p', 0, 1, 0, 0]).await.unwrap();
        assert!(password_frame(&mut stream.into()).await.is_err());
    }
    #[tokio::test]
    async fn backend_rejects_downgrades_nonce_mismatch_and_forged_server_proof() {
        let (_peer, stream) = crate::tests::socket_pair().await;
        let mut stream = stream.into();
        let keys = Keys {
            client: [1; 32],
            server: [2; 32],
        };
        let mut auth = BackendAuth::new(keys).unwrap();
        assert!(auth
            .respond(&mut stream, &3u32.to_be_bytes())
            .await
            .is_err());
        auth.phase = 1;
        let mut message = 11u32.to_be_bytes().to_vec();
        message.extend_from_slice(b"r=wrong,s=c2FsdA==,i=4096");
        assert!(auth.respond(&mut stream, &message).await.is_err());
        auth.auth = Some("transcript".into());
        auth.phase = 2;
        let mut message = 12u32.to_be_bytes().to_vec();
        message.extend_from_slice(format!("v={}", STANDARD.encode([0; 32])).as_bytes());
        assert!(auth.respond(&mut stream, &message).await.is_err());
        assert!(!auth.verified());
    }
}
