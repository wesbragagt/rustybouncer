//! Frozen pre-optimization classifier oracle, compiled only by tests.
use super::{cte, Kind};
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

pub(super) fn classify(sql: &str) -> Kind {
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
