//! Structural validation of every CTE/subquery before enabling read routing.
use sqlparser::{
    ast::{Expr, Query, Select, SetExpr, Statement, TableFactor, Visit, Visitor},
    dialect::PostgreSqlDialect,
    parser::Parser,
};
use std::ops::ControlFlow;

fn read_body(body: &SetExpr) -> bool {
    match body {
        SetExpr::Select(select) => select.into.is_none(),
        SetExpr::Query(query) => read_body(&query.body),
        SetExpr::SetOperation { left, right, .. } => read_body(left) && read_body(right),
        SetExpr::Values(_) => true,
        _ => false,
    }
}

struct ReadOnly;
impl Visitor for ReadOnly {
    type Break = ();

    fn pre_visit_query(&mut self, query: &Query) -> ControlFlow<()> {
        if !query.locks.is_empty() || query.for_clause.is_some() || !read_body(&query.body) {
            return ControlFlow::Break(());
        }
        ControlFlow::Continue(())
    }

    fn pre_visit_select(&mut self, select: &Select) -> ControlFlow<()> {
        if select.into.is_some() {
            return ControlFlow::Break(());
        }
        ControlFlow::Continue(())
    }

    fn pre_visit_statement(&mut self, statement: &Statement) -> ControlFlow<()> {
        if !matches!(statement, Statement::Query(_)) {
            return ControlFlow::Break(());
        }
        ControlFlow::Continue(())
    }

    fn pre_visit_expr(&mut self, expr: &Expr) -> ControlFlow<()> {
        if let Expr::Function(function) = expr {
            let name = function.name.to_string().to_ascii_uppercase();
            let name = name.strip_prefix("PG_CATALOG.").unwrap_or(&name);
            if !super::allowed_function(name) {
                return ControlFlow::Break(());
            }
        }
        ControlFlow::Continue(())
    }

    fn pre_visit_table_factor(&mut self, table: &TableFactor) -> ControlFlow<()> {
        // Table-valued functions have separate AST nodes from Expr::Function.
        match table {
            TableFactor::Table { args: None, .. }
            | TableFactor::Derived { .. }
            | TableFactor::NestedJoin { .. } => ControlFlow::Continue(()),
            _ => ControlFlow::Break(()),
        }
    }
}

pub(super) fn read_only(sql: &str) -> bool {
    let Ok(statements) = Parser::parse_sql(&PostgreSqlDialect {}, sql) else {
        return false;
    };
    if statements.len() != 1 {
        return false;
    }
    let Statement::Query(query) = &statements[0] else {
        return false;
    };
    query.with.is_some() && statements[0].visit(&mut ReadOnly).is_continue()
}
