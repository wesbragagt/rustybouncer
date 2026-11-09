//! Allocation-free lexical summary. CTE safety still requires the existing AST.
use super::{cte, Kind};

#[derive(Default)]
struct Summary {
    first: Option<Kind>,
    locking: bool,
    mutating: bool,
}
impl Summary {
    fn observe(&mut self, word: &str) {
        if self.first.is_none() {
            self.first = Some(match word {
                "SELECT" | "WITH" => Kind::Read,
                "INSERT" | "UPDATE" | "DELETE" | "MERGE" => Kind::Write,
                "BEGIN" | "START" | "COMMIT" | "END" | "ROLLBACK" | "ABORT" | "SAVEPOINT"
                | "RELEASE" => Kind::Transaction,
                "COPY" => Kind::Unsupported,
                _ => Kind::Stateful,
            });
        }
        self.locking |= matches!(word, "INTO" | "FOR" | "LOCK" | "OPERATOR");
        self.mutating |= matches!(
            word,
            "INSERT" | "UPDATE" | "DELETE" | "MERGE" | "CREATE" | "DROP" | "ALTER" | "SET"
        );
    }
    fn kind(self) -> Kind {
        match self.first.unwrap_or(Kind::Stateful) {
            Kind::Read if self.locking || self.mutating => Kind::Stateful,
            kind => kind,
        }
    }
}

fn string_end(bytes: &[u8], mut index: usize) -> Option<usize> {
    index += 1;
    loop {
        match *bytes.get(index)? {
            b'\\' => return None,
            b'\'' => {
                index += 1;
                if bytes.get(index) != Some(&b'\'') {
                    return Some(index);
                }
            }
            _ => {}
        }
        index += 1;
    }
}

pub(super) fn allowed_function(word: &str) -> bool {
    matches!(
        word,
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

fn observe(
    sql: &str,
    start: usize,
    end: usize,
    previous_catalog: bool,
    is_cte: bool,
    summary: &mut Summary,
) -> Option<bool> {
    // All relevant keywords fit here. A longer identifier is not a keyword or
    // allowed function, exactly as in the original uppercase-string classifier.
    let bytes = &sql.as_bytes()[start..end];
    let mut upper = [0u8; 32];
    let word = if bytes.len() <= upper.len() {
        for (output, input) in upper.iter_mut().zip(bytes) {
            *output = input.to_ascii_uppercase();
        }
        std::str::from_utf8(&upper[..bytes.len()]).ok()?
    } else {
        ""
    };
    if sql[end..].trim_start().starts_with('(') {
        if sql[..start].trim_end().ends_with('.') && !previous_catalog {
            return None;
        }
        if !is_cte
            && !allowed_function(word)
            && !matches!(word, "IN" | "EXISTS" | "ANY" | "ALL" | "AS" | "VALUES")
        {
            return None;
        }
    }
    summary.observe(word);
    Some(word == "PG_CATALOG")
}

fn scan(sql: &str, is_cte: bool) -> Option<Summary> {
    let bytes = sql.as_bytes();
    let mut summary = Summary::default();
    let mut previous_catalog = false;
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'\'' => index = string_end(bytes, index)?,
            b'"' | b'$' => return None,
            b'-' if bytes.get(index + 1) == Some(&b'-') => return None,
            b'/' if bytes.get(index + 1) == Some(&b'*') => return None,
            b';' => {
                if !sql[index + 1..].trim().is_empty() {
                    return None;
                }
                index += 1;
            }
            byte if byte.is_ascii_alphabetic() || byte == b'_' => {
                let start = index;
                while index < bytes.len()
                    && (bytes[index].is_ascii_alphanumeric() || bytes[index] == b'_')
                {
                    index += 1;
                }
                previous_catalog =
                    observe(sql, start, index, previous_catalog, is_cte, &mut summary)?;
            }
            byte if !byte.is_ascii() => return None,
            _ => index += 1,
        }
    }
    Some(summary)
}

pub(super) fn classify(sql: &str) -> Kind {
    let is_cte = sql
        .trim_start()
        .split(|c: char| !c.is_ascii_alphanumeric() && c != '_')
        .next()
        .is_some_and(|word| word.eq_ignore_ascii_case("WITH"));
    if is_cte && !cte::read_only(sql) {
        return Kind::Stateful;
    }
    scan(sql, is_cte)
        .map(Summary::kind)
        .unwrap_or(Kind::Stateful)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::router::classifier_reference;

    #[test]
    fn adversarial_mutations_preserve_the_frozen_classifier_contract() {
        let statements = [
            "SELECT 1",
            "SELECT payload FROM comparison WHERE id = 42",
            "SELECT pg_catalog.count(*) FROM items",
            "SELECT current_setting('cluster_name')",
            "SELECT 'INSERT UPDATE FOR'",
            "SELECT nextval('seq')",
            "SELECT evil.count(*)",
            "WITH x AS (SELECT 1) SELECT * FROM x",
            "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
            "BEGIN",
            "INSERT INTO t VALUES (1)",
            "COPY t TO STDOUT",
        ];
        let prefixes = [
            "",
            " ",
            "/* hidden */ ",
            "-- hidden\n",
            "'a' ",
            "EXPLAIN ",
            "WITH ",
            "SELECT ",
        ];
        let suffixes = [
            "",
            ";",
            "; SELECT 2",
            " FOR UPDATE",
            " /* hidden */",
            " $body$",
            " ",
            " INTO other",
        ];
        for statement in statements {
            for prefix in prefixes {
                for suffix in suffixes {
                    let sql = format!("{prefix}{statement}{suffix}");
                    for sql in [&sql, &sql.to_ascii_lowercase(), &sql.to_ascii_uppercase()] {
                        assert_eq!(classify(sql), classifier_reference::classify(sql), "{sql}");
                    }
                }
            }
        }
        for length in 1..=128 {
            let identifier = "a".repeat(length);
            for sql in [
                format!("SELECT {identifier} FROM items"),
                format!("SELECT {identifier}()"),
                format!("SELECT {identifier}.count(*)"),
                format!("WITH {identifier} AS (SELECT 1) SELECT * FROM {identifier}"),
            ] {
                assert_eq!(
                    classify(&sql),
                    classifier_reference::classify(&sql),
                    "{sql}"
                );
            }
        }
        let mut seed = 17u32;
        for _ in 0..2048 {
            let mut sql = String::new();
            for _ in 0..64 {
                seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
                sql.push(char::from((seed >> 24) as u8 & 127));
            }
            assert_eq!(
                classify(&sql),
                classifier_reference::classify(&sql),
                "{sql:?}"
            );
        }
    }
    #[test]
    fn unsafe_inputs_never_become_retryable_reads() {
        for sql in [
            "SELECT nextval('s')",
            "SELECT set_config('search_path','evil',false)",
            "SELECT custom_func()",
            "SELECT evil.count(*)",
            "SELECT \"count\"(*)",
            "SELECT * FROM items FOR UPDATE",
            "SELECT * INTO stolen FROM items",
            "SELECT 1 OPERATOR(public.===) 2",
            "SELECT 1; INSERT INTO t VALUES(1)",
            "WITH x AS (INSERT INTO t VALUES(1) RETURNING *) SELECT * FROM x",
            "WITH x AS (WITH y AS (SELECT custom_func()) SELECT * FROM y) SELECT * FROM x",
            "SELECT /* ambiguity */ 1",
            "SELECT $$hidden$$",
            "SELECT 'a\\b'",
        ] {
            assert_ne!(classify(sql), Kind::Read, "{sql}");
        }
    }
    fn measured(queries: &[String], classifier: fn(&str) -> Kind) -> u128 {
        let began = std::time::Instant::now();
        let mut reads = 0;
        for _ in 0..10 {
            for index in 0..queries.len() {
                let sql = &queries[index * 7919 % queries.len()];
                reads += usize::from(classifier(std::hint::black_box(sql)) == Kind::Read);
            }
        }
        let elapsed = began.elapsed().as_nanos();
        assert_eq!(reads, queries.len() * 10);
        elapsed
    }
    #[test]
    fn record_literal_and_cte_classification_cost_without_a_performance_budget() {
        let literal = (1..=10000)
            .map(|key| format!("SELECT payload FROM comparison WHERE id = {key};"))
            .collect::<Vec<_>>();
        let ctes=(1..=1000).map(|key|format!("WITH chosen AS (SELECT payload FROM comparison WHERE id = {key}) SELECT payload FROM chosen;")).collect::<Vec<_>>();
        for (name, queries) in [("literal", &literal), ("cte", &ctes)] {
            for repetition in 1..=3 {
                let (legacy, candidate) = if repetition % 2 == 1 {
                    (
                        measured(queries, classifier_reference::classify),
                        measured(queries, classify),
                    )
                } else {
                    let candidate = measured(queries, classify);
                    (measured(queries, classifier_reference::classify), candidate)
                };
                println!("CLASSIFICATION_PROFILE workload={name} repetition={repetition} calls={} legacy_ns={legacy} candidate_ns={candidate}",queries.len()*10);
            }
        }
    }
}
