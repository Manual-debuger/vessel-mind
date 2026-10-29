"""The vector store's day-one risk (docs/specs/a-agent-track.md §2).

sqlite-vec is a loadable extension, and some Python builds disable
sqlite3.Connection.enable_load_extension. This test proves the extension loads under
the interpreter that runs the suite, and answers a nearest-neighbour query.
"""

import sqlite3

import sqlite_vec


def test_sqlite_vec_loads_and_finds_the_nearest_vector():
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)

    db.execute("CREATE VIRTUAL TABLE vectors USING vec0(embedding float[2])")
    db.executemany(
        "INSERT INTO vectors(rowid, embedding) VALUES (?, ?)",
        [(1, "[1.0, 0.0]"), (2, "[0.0, 1.0]"), (3, "[0.9, 0.1]")],
    )
    rows = db.execute(
        "SELECT rowid FROM vectors WHERE embedding MATCH ? ORDER BY distance LIMIT 2",
        ["[0.0, 0.9]"],
    ).fetchall()

    assert rows == [(2,), (3,)]
