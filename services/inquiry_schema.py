"""Additive, repeatable listing opt-in migration; existing false stays false."""
from sqlalchemy import inspect, text


def ensure_inquiry_schema(engine):
    with engine.begin() as connection:
        if engine.dialect.name == 'postgresql':
            connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            connection.execute(text("SET LOCAL statement_timeout = '60s'"))
            connection.execute(text('SELECT pg_advisory_xact_lock(683937122)'))
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        for table in ('orders', 'p2p_offers'):
            if table not in tables:
                continue
            columns = {column['name']: column for column in inspector.get_columns(table)}
            if 'allow_pretrade_chat' not in columns:
                connection.execute(text(f'ALTER TABLE {table} ADD COLUMN allow_pretrade_chat BOOLEAN NOT NULL DEFAULT TRUE'))
            else:
                # Repair partial earlier migrations, never overwrite opt-outs.
                column = columns['allow_pretrade_chat']
                if column.get('nullable'):
                    connection.execute(text(f'UPDATE {table} SET allow_pretrade_chat = TRUE WHERE allow_pretrade_chat IS NULL'))
                if engine.dialect.name == 'postgresql':
                    if str(column.get('default', '')).lower() not in ('true', 'true::boolean'):
                        connection.execute(text(f'ALTER TABLE {table} ALTER COLUMN allow_pretrade_chat SET DEFAULT TRUE'))
                    if column.get('nullable'):
                        connection.execute(text(f'ALTER TABLE {table} ALTER COLUMN allow_pretrade_chat SET NOT NULL'))
