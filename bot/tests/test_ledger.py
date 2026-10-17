"""Diario sellado: cambiar o borrar una operación cerrada se detecta (puerta de fase confiable)."""
from bot.learning.journal import Journal
from bot.tests.test_learning_metrics import mk_trade


def test_ledger_detects_edits_and_deletions(tmp_path):
    j = Journal(tmp_path / "j.sqlite")
    for i in range(3):
        j.record_trade(mk_trade(i, 1.0 + i))
    v = j.verify_ledger()
    assert v["ok"] and v["entries"] == 3 and v["unsealed"] == 0
    tid = j.db.execute("SELECT id FROM trades LIMIT 1").fetchone()[0]
    orig = j.db.execute("SELECT pnl_net FROM trades WHERE id=?", (tid,)).fetchone()[0]
    j.db.execute("UPDATE trades SET pnl_net = 999 WHERE id=?", (tid,))  # "maquillar" un resultado
    v = j.verify_ledger()
    assert not v["ok"] and v["altered"] == [tid]
    j.db.execute("UPDATE trades SET pnl_net = ? WHERE id=?", (orig, tid))
    assert j.verify_ledger()["ok"]
    j.db.execute("DELETE FROM trades WHERE id=?", (tid,))
    assert j.verify_ledger()["missing"] == [tid]
    j.db.execute("UPDATE ledger SET digest='x' WHERE seq=2")
    assert j.verify_ledger()["broken_at"] == 2
