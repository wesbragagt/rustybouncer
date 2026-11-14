-- Apply as the database owner/superuser in the auth_query database (app).
-- Issue a CNPG client certificate whose CN is rustybouncer_auth. This role
-- cannot read pg_authid directly and is never used for application queries.
CREATE ROLE rustybouncer_auth LOGIN;
GRANT CONNECT ON DATABASE app TO rustybouncer_auth;
CREATE SCHEMA pooler;
CREATE FUNCTION pooler.auth_query(name)
RETURNS TABLE(username name, password text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT rolname,
           CASE WHEN rolvaliduntil IS NULL OR rolvaliduntil > now()
                THEN rolpassword END
    FROM pg_authid WHERE rolname = $1 AND rolcanlogin
$$;
REVOKE ALL ON FUNCTION pooler.auth_query(name) FROM PUBLIC;
GRANT USAGE ON SCHEMA pooler TO rustybouncer_auth;
GRANT EXECUTE ON FUNCTION pooler.auth_query(name) TO rustybouncer_auth;
