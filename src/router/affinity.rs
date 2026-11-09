//! Session-affinity analysis is distinct from replica/retry purity.
//! Known primary-only observations/parameter queries may release an idle lease;
//! unknown functions or session effects retain ownership until frontend close.
use super::Kind;
use sqlparser::{
    ast::{ContextModifier, Expr, Select, Set, Statement, TableFactor, Visit, Visitor},
    dialect::PostgreSqlDialect,
    parser::Parser,
};
use std::ops::ControlFlow;

fn known_function(name: &sqlparser::ast::ObjectName) -> bool {
    let name = name.to_string().to_ascii_uppercase();
    let name = name.strip_prefix("PG_CATALOG.").unwrap_or(&name);
    super::allowed_function(name)
        || matches!(
            name,
            "PG_BACKEND_PID"
                | "CURRENT_DATABASE"
                | "CURRENT_USER"
                | "SESSION_USER"
                | "PG_SLEEP"
                | "PG_SLEEP_FOR"
                | "PG_SLEEP_UNTIL"
                | "MD5"
                | "REPEAT"
        )
}

struct LocalOnly;
impl Visitor for LocalOnly {
    type Break = ();
    fn pre_visit_statement(&mut self, statement: &Statement) -> ControlFlow<()> {
        let safe = match statement {
            Statement::Query(_)
            | Statement::Insert(_)
            | Statement::Update(_)
            | Statement::Delete(_)
            | Statement::ShowVariable { .. }
            | Statement::StartTransaction { .. }
            | Statement::Commit { .. }
            | Statement::Rollback { .. }
            | Statement::Savepoint { .. }
            | Statement::ReleaseSavepoint { .. } => true,
            Statement::Set(set) => matches!(
                set,
                Set::SingleAssignment {
                    scope: Some(ContextModifier::Local),
                    ..
                } | Set::SetRole {
                    context_modifier: Some(ContextModifier::Local),
                    ..
                } | Set::SetTimeZone { local: true, .. }
                    | Set::SetTransaction { session: false, .. }
            ),
            _ => false,
        };
        if safe {
            ControlFlow::Continue(())
        } else {
            ControlFlow::Break(())
        }
    }
    fn pre_visit_select(&mut self, select: &Select) -> ControlFlow<()> {
        if select.into.is_some() {
            ControlFlow::Break(())
        } else {
            ControlFlow::Continue(())
        }
    }
    fn pre_visit_expr(&mut self, expression: &Expr) -> ControlFlow<()> {
        if let Expr::Function(function) = expression {
            if !known_function(&function.name) {
                return ControlFlow::Break(());
            }
        }
        ControlFlow::Continue(())
    }
    fn pre_visit_table_factor(&mut self, table: &TableFactor) -> ControlFlow<()> {
        match table {
            TableFactor::Table { args: None, .. }
            | TableFactor::Derived { .. }
            | TableFactor::NestedJoin { .. } => ControlFlow::Continue(()),
            TableFactor::Table {
                name,
                args: Some(_),
                ..
            }
            | TableFactor::Function { name, .. }
                if known_function(name) =>
            {
                ControlFlow::Continue(())
            }
            _ => ControlFlow::Break(()),
        }
    }
}

pub(super) fn session_required(sql: &str, kind: Kind) -> bool {
    if !matches!(kind, Kind::Stateful | Kind::Write) {
        return false;
    }
    let Ok(statements) = Parser::parse_sql(&PostgreSqlDialect {}, sql) else {
        return true;
    };
    statements.is_empty()
        || statements
            .iter()
            .any(|statement| statement.visit(&mut LocalOnly).is_break())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn primary_only_observations_and_local_state_do_not_become_retryable_reads() {
        for sql in [
            "SELECT pg_backend_pid()",
            "SELECT current_user,current_database()",
            "SELECT pg_catalog.pg_backend_pid(), $1::integer",
            "SELECT $1::integer + 1, $2::text",
            "SELECT pg_sleep(1)",
            "SET LOCAL application_name = 'transaction'",
            "SET LOCAL ROLE postgres",
            "SET LOCAL TIME ZONE 'UTC'",
            "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
        ] {
            let kind = super::super::classify(sql);
            assert_eq!(kind, Kind::Stateful, "original routing changed: {sql}");
            assert!(!session_required(sql, kind), "unnecessarily pinned: {sql}");
        }
    }
    #[test]
    fn dml_releases_at_idle_but_explicit_session_functions_retain_state() {
        for sql in [
            "INSERT INTO items VALUES(1,'x')",
            "UPDATE items SET value='x'",
            "DELETE FROM items WHERE id=1",
        ] {
            assert!(
                !session_required(sql, Kind::Write),
                "DML permanently pinned: {sql}"
            );
        }
        assert!(session_required(
            "INSERT INTO items SELECT 1,set_config('application_name','x',false)",
            Kind::Write
        ));
        assert!(session_required(
            "UPDATE items SET value=unknown_session_function()",
            Kind::Write
        ));
    }
    #[test]
    fn unknown_session_effects_and_quoted_function_names_retain_affinity() {
        for sql in [
            "SET application_name='session'",
            "SET SESSION AUTHORIZATION alice",
            "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL SERIALIZABLE",
            "PREPARE x AS SELECT 1",
            "LISTEN changes",
            "CREATE TEMP TABLE x(id int)",
            "SELECT pg_advisory_lock(1)",
            "SELECT set_config('application_name','x',false)",
            "SELECT app.pg_backend_pid()",
            "SELECT \"PG_BACKEND_PID\"()",
            "SELECT unknown()",
            "SELECT pg_sleep(1); SET application_name='session'",
            "SELECT 1 INTO TEMP x",
        ] {
            assert!(
                session_required(sql, Kind::Stateful),
                "lost session state: {sql}"
            );
        }
    }
}
