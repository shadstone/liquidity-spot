"""Release startup checks without importing run.py or connecting to a database."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import Numeric, text
from services.p2p_schema import ensure_payment_schema


def fake_engine(dialect='postgresql'):
    engine = MagicMock()
    engine.dialect.name = dialect
    connection = engine.begin.return_value.__enter__.return_value
    return engine, connection


class ReleaseSchemaTests(unittest.TestCase):
    def test_payment_migration_bounds_waits_before_advisory_lock(self):
        engine, connection = fake_engine()
        inspector = MagicMock()
        inspector.get_table_names.return_value = []
        with patch('services.p2p_schema.inspect', return_value=inspector):
            ensure_payment_schema(engine)
        statements = [str(call.args[0]) for call in connection.execute.call_args_list]
        self.assertEqual(statements, [
            "SET LOCAL lock_timeout = '10s'",
            "SET LOCAL statement_timeout = '60s'",
            'SELECT pg_advisory_xact_lock(683937120)',
        ])

    def test_payment_lock_error_propagates_for_fail_fast_startup(self):
        engine, connection = fake_engine()
        connection.execute.side_effect = [None, None, RuntimeError('lock timeout')]
        with patch('services.p2p_schema.inspect') as inspector:
            with self.assertRaisesRegex(RuntimeError, 'lock timeout'):
                ensure_payment_schema(engine)
        inspector.assert_not_called()

    def test_sqlite_does_not_receive_postgresql_settings(self):
        engine, connection = fake_engine('sqlite')
        inspector = MagicMock()
        inspector.get_table_names.return_value = []
        with patch('services.p2p_schema.inspect', return_value=inspector):
            ensure_payment_schema(engine)
        connection.execute.assert_not_called()

    def numeric_upgrade(self, engine, inspector):
        # Execute only the helper's AST: importing run.py would run app startup
        # and environment-backed migrations, which these unit tests must avoid.
        source = Path(__file__).resolve().parents[1] / 'run.py'
        module = ast.parse(source.read_text())
        function = next(node for node in module.body
                        if isinstance(node, ast.FunctionDef) and node.name == 'ensure_numeric_precision')
        isolated = ast.Module(body=[function], type_ignores=[])
        scope = {'db': SimpleNamespace(engine=engine), 'inspect': inspector, 'text': text, 'Numeric': Numeric}
        exec(compile(isolated, str(source), 'exec'), scope)
        scope['ensure_numeric_precision']()

    def test_numeric_upgrade_skips_already_correct_types(self):
        engine, connection = fake_engine()
        inspector = MagicMock()
        inspector.return_value.get_columns.return_value = [
            {'name': 'amount_hns', 'type': Numeric(24, 8)},
            {'name': 'price_btc_per_hns', 'type': Numeric(24, 12)},
        ]
        self.numeric_upgrade(engine, inspector)
        connection.execute.assert_not_called()

    def test_numeric_upgrade_only_changes_mismatched_column(self):
        engine, connection = fake_engine()
        inspector = MagicMock()
        inspector.return_value.get_columns.side_effect = [
            [{'name': 'amount_hns', 'type': Numeric(24, 8)},
             {'name': 'price_btc_per_hns', 'type': Numeric(18, 8)}],
            [{'name': 'amount_hns', 'type': Numeric(24, 8)},
             {'name': 'price_btc_per_hns', 'type': Numeric(24, 12)}],
        ]
        self.numeric_upgrade(engine, inspector)
        self.assertEqual([str(call.args[0]) for call in connection.execute.call_args_list], [
            'ALTER TABLE orders ALTER COLUMN price_btc_per_hns TYPE NUMERIC(24,12)',
        ])

    def test_numeric_upgrade_leaves_non_postgresql_alone(self):
        engine, connection = fake_engine('sqlite')
        inspector = MagicMock()
        self.numeric_upgrade(engine, inspector)
        engine.begin.assert_not_called()
        inspector.assert_not_called()


if __name__ == '__main__':
    unittest.main()
