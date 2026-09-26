"""The local web UI (spec/09-WEB.md §9).

Against the two-game fixture database `test_query.QueryBase` builds, never
against a stub. That database has lineups and comments but no reference
tables and no archive, which makes it the degraded case of §8 for free; the
ambiguous-name tests add reference rows to a copy of it.
"""

import argparse
import http.client
import json
import re
import shutil
import threading
import unittest
from pathlib import Path
from unittest import mock

from rsse import cli
from rsse.query import QueryError, Search, connect
from rsse.query import params as qp
from rsse.web import api, server

from tests.test_query import QueryBase

ATTR = "attribution notice"

#: A value of the right shape for every kind of parameter, so the agreement
#: test covers the whole table rather than the entries someone remembered.
SAMPLE = {"int": "2", "str": "x", "choice": None, "range": "1990,2000",
          "seq": "2,1", "list": ["Strikeout"], "switch": True}

#: Values that make each composite legal on its own.
COMPOSITE_NEEDS = {"force_certainty": {"force_play": True},
                   "include_tag_outs": {"force_play": True},
                   "assist_by": {"putout_by": "1"}}


#: Parameters whose method validates the value itself.
SAMPLE_FOR = {"bases": "101", "position_played": "SS"}


def sample_params(p):
    value = SAMPLE_FOR.get(p.name) or (
        p.choices[0] if p.kind == "choice" else SAMPLE[p.kind])
    return {p.name: value, **COMPOSITE_NEEDS.get(p.name, {})}


def argv_for(params):
    argv = []
    for name, value in params.items():
        flag = qp.BY_NAME[name].flag
        if value is True:
            argv.append(flag)
        elif isinstance(value, list):
            for v in value:
                argv += [flag, v]
        else:
            argv += [flag, str(value)]
    return argv


def cli_parser():
    parser = argparse.ArgumentParser()
    cli._add_query_flags(parser)
    return parser


def page_code() -> str:
    """app.js without its comments, which are allowed to name what the code
    must not do."""
    js = server.static_bytes("app.js").decode()
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"^\s*//.*$", "", js, flags=re.M)


class WebBase(QueryBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batter = cls.conn.execute(
            "SELECT batter_id FROM plays ORDER BY play_id LIMIT 1").fetchone()[0]
        cls.play_id, cls.game_key = cls.conn.execute(
            "SELECT play_id, game_key FROM plays ORDER BY play_id LIMIT 1"
            " OFFSET 10").fetchone()
        cls.app = server.App(server.Config(database=cls.db, attribution=ATTR))

    def run_query(self, body, app=None):
        app = app or self.app
        rid = app.issue()
        return app.run_search(rid, lambda c: api.query(c, body, ATTR))


class ParameterTable(WebBase):
    """§2: one table; the CLI and the form cannot disagree."""

    def test_cli_and_form_compile_identically_for_every_parameter(self):
        parser = cli_parser()
        for p in qp.QUERY_PARAMS:
            params = sample_params(p)
            with self.subTest(param=p.name):
                args = parser.parse_args(argv_for(params))
                from_cli = cli._search_from_args(args)._compile("p.play_id")
                from_form = qp.search_from_params(params)._compile("p.play_id")
                self.assertEqual(from_cli, from_form)

    def test_the_shown_command_line_reproduces_the_query(self):
        # The export dialog says "run this for more rows"; it had better be
        # the same query.
        params = {"bases_loaded": True, "outs": "2", "tag": ["Strikeout", "TwoOuts"],
                  "force_play_at": "H", "include_tag_outs": True,
                  "putout_sequence": "2,1", "seasons": "2000,2000"}
        line = api.command_line(params, 25)
        args = cli_parser().parse_args(line.split()[2:])
        self.assertEqual(cli._search_from_args(args)._compile("p.play_id"),
                         qp.search_from_params(params)._compile("p.play_id"))

    def test_unknown_parameters_are_refused(self):
        for bad in ({"bases_load": True}, {"outz": "2"}, {"order_by": "g.date"}):
            with self.subTest(bad=bad):
                with self.assertRaises(QueryError) as caught:
                    qp.search_from_params(bad)
                self.assertIn(next(iter(bad)), str(caught.exception))
                with self.assertRaises(api.ApiError) as err:
                    self.run_query({"params": bad})
                self.assertEqual(err.exception.status, 400)

    def test_a_near_miss_is_named_with_suggestions(self):
        with self.assertRaises(QueryError) as caught:
            qp.search_from_params({"bases_load": True})
        self.assertIn("bases_loaded", str(caught.exception))

    def test_blank_form_fields_mean_not_set(self):
        self.assertEqual(qp.search_from_params({"outs": "", "tag": []}),
                         Search())

    def test_a_modifier_without_its_predicate_is_refused(self):
        with self.assertRaises(QueryError):
            qp.search_from_params({"force_certainty": "derived"})
        with self.assertRaises(QueryError):
            qp.search_from_params({"assist_by": "2"})


class NoSqlText(WebBase):
    """§2: nothing reachable from a request takes SQL."""

    def test_no_parameter_maps_to_order_by(self):
        self.assertNotIn("order_by", {p.method for p in qp.QUERY_PARAMS})

    def test_orders_are_names_not_sql(self):
        with self.assertRaises(QueryError):
            qp.apply_page(Search(), order="g.date; DROP TABLE plays")
        with self.assertRaises(api.ApiError) as err:
            self.run_query({"params": {"outs": "0"}, "order": "p.play_id"})
        self.assertEqual(err.exception.status, 400)

    def test_page_size_is_one_of_the_offered_sizes(self):
        with self.assertRaises(api.ApiError) as err:
            self.run_query({"params": {"outs": "0"}, "limit": 100000})
        self.assertEqual(err.exception.status, 400)


class Paging(WebBase):
    def test_offset_needs_a_limit(self):
        with self.assertRaises(QueryError):
            Search().offset(5).count(self.conn)

    def test_pages_partition_the_result(self):
        whole = self.run_query({"params": {"outs": "0"}, "limit": 100})
        first = self.run_query({"params": {"outs": "0"}, "limit": 25})
        second = self.run_query({"params": {"outs": "0"}, "limit": 25,
                                 "offset": 25})
        ids = [r["play_id"] for r in whole["rows"]]
        self.assertEqual([r["play_id"] for r in first["rows"]], ids[:25])
        self.assertEqual([r["play_id"] for r in second["rows"]], ids[25:50])
        # Moving to another page does not recount.
        self.assertEqual(first["total"], second["total"])
        self.assertEqual(first["total"], whole["total"])


class Disclosure(WebBase):
    """§4: a count is never shown without what was searched."""

    def test_an_empty_result_carries_coverage(self):
        result = self.run_query({"params": {"triple_play": True,
                                            "walkoff": True}})
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["rows"], [])
        self.assertGreater(result["coverage"]["games"], 0)
        self.assertTrue(result["describe"]["coverage"])
        self.assertTrue(result["describe"]["excluded"])

    def test_the_page_words_an_empty_result_against_coverage(self):
        # One render function for every result; it must state the searched
        # games, and nothing in the page may say a bare "no results".
        js = page_code()
        self.assertEqual(js.count("No matching plays in"), 1)
        self.assertIsNone(re.search(r"no results", js, re.I))

    def test_a_force_query_carries_the_force_report(self):
        result = self.run_query({"params": {"force_play": True}})
        self.assertIsNotNone(result["force"])
        self.assertIn("untrusted-state", result["describe"]["force"])

    def test_results_carry_attribution_and_versions(self):
        result = self.run_query({"params": {"outs": "2"}})
        self.assertEqual(result["attribution"], ATTR)
        self.assertTrue(result["corpus_version"])
        self.assertTrue(result["ontology_version"])

    def test_export_uses_the_cli_writer_and_carries_attribution(self):
        body = {"params": {"outs": "2"}, "limit": 25}
        ctype, data = api.export(self.conn, body, "csv", ATTR)
        text = data.decode()
        self.assertTrue(ctype.startswith("text/csv"))
        self.assertTrue(text.startswith("game_id,date,"))
        self.assertTrue(text.rstrip().endswith(ATTR))
        ctype, data = api.export(self.conn, body, "json", ATTR)
        self.assertEqual(json.loads(data)["attribution"], ATTR)


class Deadline(WebBase):
    """§6: a stopped query returns an error, never a short list."""

    def test_a_timeout_returns_no_rows(self):
        app = server.App(server.Config(database=self.db, timeout=0,
                                       attribution=ATTR))
        with mock.patch.object(server, "PROGRESS_STEP", 1):
            with self.assertRaises(api.ApiError) as err:
                self.run_query({"params": {"outs": "0"}}, app)
        self.assertEqual(err.exception.status, 504)
        self.assertNotIn("rows", err.exception.as_dict())

    def test_cancel_interrupts_and_returns_no_rows(self):
        app = server.App(server.Config(database=self.db, attribution=ATTR))
        rid = app.issue()
        app.cancel(rid)
        with self.assertRaises(api.ApiError) as err:
            app.run_search(rid, lambda c: api.query(
                c, {"params": {"outs": "0"}}, ATTR))
        self.assertEqual(err.exception.status, 499)


class RequestIds(WebBase):
    """§6: ids come from the server and are good for one search."""

    def test_an_id_the_server_did_not_issue_is_refused(self):
        with self.assertRaises(api.ApiError) as err:
            self.app.run_search("made-up", lambda c: None)
        self.assertEqual(err.exception.status, 404)
        with self.assertRaises(api.ApiError):
            self.app.cancel("made-up")

    def test_an_id_is_used_once(self):
        rid = self.app.issue()
        self.app.run_search(rid, lambda c: api.query(
            c, {"params": {"outs": "2"}}, ATTR))
        with self.assertRaises(api.ApiError) as err:
            self.app.run_search(rid, lambda c: None)
        self.assertEqual(err.exception.status, 404)

    def test_ids_are_not_guessable(self):
        ids = {self.app.issue() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(len(i) >= 20 for i in ids))


class ConcurrencyCap(WebBase):
    """§6: searches queue; browse pages do not queue behind them."""

    def test_a_search_waits_then_gets_503_and_browsing_still_works(self):
        app = server.App(server.Config(database=self.db, max_queries=1,
                                       attribution=ATTR))
        app.slots.acquire()                 # one search already running
        try:
            with mock.patch.object(server, "QUEUE_WAIT", 0.05):
                with self.assertRaises(api.ApiError) as err:
                    self.run_query({"params": {"outs": "2"}}, app)
            self.assertEqual(err.exception.status, 503)
            page = app.browse(lambda c: api.game(c, self.game_key))
            self.assertEqual(page["game"]["game_key"], self.game_key)
        finally:
            app.slots.release()
        # And with the slot free the same search runs.
        self.assertIn("total", self.run_query({"params": {"outs": "2"}}, app))


class Names(WebBase):
    """§7: ambiguity returns candidates as data."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.refdb = cls.tmp / "ref.db"
        shutil.copyfile(cls.db, cls.refdb)
        ref = connect(str(cls.refdb), read_only=False)
        from rsse.database.reference import REFERENCE_DDL, REFERENCE_INDEXES
        ref.executescript(REFERENCE_DDL)
        ref.executescript(REFERENCE_INDEXES)
        ref.executemany(
            "INSERT INTO people (person_id, last, first, nickname, birthdate,"
            " play_debut, source) VALUES (?, ?, ?, ?, ?, ?, 'bio')",
            [(cls.batter, "Dupe", "Sam", None, "01/01/1970", "04/01/1995"),
             ("dupes999", "Dupe", "Sam", None, "01/01/1880", "04/01/1902")])
        ref.commit()
        ref.close()
        cls.refapp = server.App(server.Config(database=cls.refdb,
                                              attribution=ATTR))

    def test_query_error_carries_candidates(self):
        conn = connect(str(self.refdb))
        with self.assertRaises(QueryError) as caught:
            Search().batter_named("Sam Dupe").count(conn)
        self.assertEqual({c[1] for c in caught.exception.candidates},
                         {self.batter, "dupes999"})

    def test_ambiguity_is_409_with_candidates(self):
        with self.assertRaises(api.ApiError) as err:
            self.run_query({"params": {"batter_named": "Sam Dupe"}},
                           self.refapp)
        self.assertEqual(err.exception.status, 409)
        ids = {c["id"] for c in err.exception.extra["candidates"]}
        self.assertEqual(ids, {self.batter, "dupes999"})

    def test_choosing_a_candidate_is_the_same_as_passing_the_id(self):
        chosen = self.run_query({"params": {"batter": self.batter}},
                                self.refapp)
        direct = qp.search_from_params({"batter": self.batter}).limit(25).run(
            connect(str(self.refdb)))
        self.assertEqual(chosen["total"], direct.total)
        self.assertGreater(chosen["total"], 0)

    def test_a_name_that_matches_nothing_is_400_not_409(self):
        with self.assertRaises(api.ApiError) as err:
            self.run_query({"params": {"batter_named": "Nobody Atall"}},
                           self.refapp)
        self.assertEqual(err.exception.status, 400)

    def test_prefix_search_finds_by_surname_and_first_name(self):
        conn = connect(str(self.refdb))
        self.assertEqual(len(api.names(conn, "dup")["people"]), 2)
        self.assertEqual(len(api.names(conn, "Dupe")["people"]), 2)
        self.assertEqual(len(api.names(conn, "sam dup")["people"]), 2)
        self.assertEqual(api.names(conn, "tom dup")["people"], [])

    def test_the_surname_arm_uses_the_index(self):
        conn = connect(str(self.refdb))
        seen = []
        conn.set_trace_callback(seen.append)
        api.names(conn, "Dup")
        people = [s for s in seen if "FROM people" in s]
        plan = " ".join(r[-1] for r in conn.execute(
            "EXPLAIN QUERY PLAN " + people[0]))
        self.assertIn("ix_people_name", plan)


class Degraded(WebBase):
    """§8: every endpoint answers without the optional tables."""

    def test_every_page_answers_and_names_the_missing_command(self):
        c = self.conn
        pages = {
            "schema": api.schema(c),
            "names": api.names(c, "wal"),
            "play": api.play(c, self.play_id),
            "game": api.game(c, self.game_key),
            "player": api.player(c, self.batter),
            "team": api.team(c, "MIN"),
            "team_season": api.team_season(c, "MIN", 2000),
            "date": api.date(c, "2000-01-01"),
            "season": api.season(c, 2000),
            "seasons": api.seasons(c),
            "about": api.about(c, ATTR),
        }
        self.assertEqual(pages["names"]["people"], [])
        self.assertEqual(pages["player"]["person"]["build"], "rsse reference")
        self.assertEqual(pages["team"]["seasons"]["missing_table"], "teams")
        self.assertIn("gamelogs", pages["game"]["game_log"]["build"])
        self.assertIn("rsse ingest", pages["play"]["source"]["build"])
        self.assertFalse(pages["schema"]["tables"]["people"])
        json.dumps(pages, default=str)      # all of it serialisable

    def test_missing_ids_are_404(self):
        for fn, arg in ((api.play, 10**9), (api.game, 10**9),
                        (api.player, "nobody99")):
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(api.ApiError) as err:
                    fn(self.conn, arg)
                self.assertEqual(err.exception.status, 404)


class PagesFromTheDatabase(WebBase):
    def test_line_score_sums_to_the_final(self):
        game = api.game(self.conn, self.game_key)
        ls = game["line_score"]
        self.assertEqual(ls["away_total"], sum(r or 0 for r in ls["away"]))
        self.assertTrue(ls["agrees"])

    def test_lineup_at_a_play_follows_the_timeline(self):
        # The pitcher the play page shows must be the one `.pitcher()`
        # attributes the play to (06-QUERY §2.1).
        play = api.play(self.conn, self.play_id)
        fielding = 1 - play["play"]["batting_team"]
        pitcher = [e for e in play["lineup"][fielding]["defense"]
                   if e["position"] == 1][0]["player_id"]
        sql, params = (Search().pitcher(pitcher).all_game_types()
                       .include_uncertain().include_unparsed()
                       ._compile("p.play_id", order=False))
        self.assertIn(self.play_id,
                      {r[0] for r in self.conn.execute(sql, params)})

    def test_tag_counts_equal_the_linked_queries(self):
        counts = api.tag_counts(self.conn, self.batter)["counts"]
        self.assertTrue(counts)
        for tag, by_season in counts.items():
            for season, n in by_season.items():
                with self.subTest(tag=tag, season=season):
                    self.assertEqual(
                        n, Search().batter(self.batter).season(season)
                        .tag(tag).count(self.conn))


class BrowseUsesIndexes(WebBase):
    """§9: no browse page scans `plays` or `games`, bar the one named."""

    #: `games.site` has no index and the UI adds none (§6).
    ALLOWED = {"park"}

    def plans(self, fn):
        conn = connect(str(self.db))
        seen = []
        conn.set_trace_callback(seen.append)
        try:
            fn(conn)
        except api.ApiError:
            pass
        conn.set_trace_callback(None)
        out = []
        for sql in seen:
            if not sql.lstrip().upper().startswith("SELECT") or \
                    "sqlite_master" in sql:
                continue
            out.append((sql, " | ".join(r[-1] for r in conn.execute(
                "EXPLAIN QUERY PLAN " + sql))))
        return out

    def test_no_browse_query_scans(self):
        pages = {
            "play": lambda c: api.play(c, self.play_id),
            "game": lambda c: api.game(c, self.game_key),
            "game_by_id": lambda c: api.game_by_id(c, "x"),
            "player": lambda c: api.player(c, self.batter),
            "team_season": lambda c: api.team_season(c, "MIN", 2000),
            "date": lambda c: api.date(c, "2000-04-03"),
            "season": lambda c: api.season(c, 2000),
            "park": lambda c: api.park(c, "MIN03"),
        }
        scans = re.compile(r"\bSCAN (plays|games|p|g)\b(?! USING)")
        for name, fn in pages.items():
            for sql, plan in self.plans(fn):
                with self.subTest(page=name, sql=sql[:80]):
                    if name in self.ALLOWED and "FROM games WHERE site" in sql:
                        continue
                    self.assertIsNone(scans.search(plan), plan)


class Packaging(unittest.TestCase):
    def test_the_page_is_package_data(self):
        for name, _ctype in server.STATIC.values():
            self.assertTrue(server.static_bytes(name))
        self.assertIn(b"app.js", server.static_bytes("index.html"))
        pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
        self.assertIn('"rsse.web" = ["static/*"]', pyproject)

    def test_the_page_never_assigns_html(self):
        # Source text is inserted as text only (§8).
        js = page_code()
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("outerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertNotIn("document.write", js)


class Server(WebBase):
    """One smoke test through a real socket, and the Host check (§8)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        config = server.Config(database=cls.db, attribution=ATTR,
                               allowed_hosts=frozenset({"rsse.lan"}))
        cls.httpd = server.make_server(config, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        super().tearDownClass()

    def request(self, method, path, body=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        with mock.patch.object(server.Handler, "log_message", lambda *a: None):
            conn.request(method, path, data, headers)
            res = conn.getresponse()
            payload = res.read()
        conn.close()
        return res, payload

    def test_page_schema_and_one_query(self):
        res, body = self.request("GET", "/")
        self.assertEqual(res.status, 200)
        self.assertIn(b"<main", body)
        self.assertIn("default-src 'self'",
                      res.getheader("Content-Security-Policy"))
        res, body = self.request("GET", "/api/schema")
        self.assertEqual(res.status, 200)
        self.assertTrue(json.loads(body)["params"])
        res, body = self.request("POST", "/api/query/start", {})
        rid = json.loads(body)["id"]
        res, body = self.request("POST", f"/api/query/{rid}",
                                 {"params": {"outs": "2"}})
        self.assertEqual(res.status, 200)
        self.assertIn("coverage", json.loads(body))

    def test_a_foreign_host_is_refused(self):
        res, _ = self.request("GET", "/api/schema", host="evil.example")
        self.assertEqual(res.status, 421)
        res, _ = self.request("GET", "/api/schema",
                              host=f"evil.example:{self.port}")
        self.assertEqual(res.status, 421)

    def test_an_allowed_host_is_accepted(self):
        for host in (f"localhost:{self.port}", "rsse.lan", f"rsse.lan:{self.port}"):
            with self.subTest(host=host):
                res, _ = self.request("GET", "/api/schema", host=host)
                self.assertEqual(res.status, 200)

    def test_errors_are_json_with_status(self):
        res, body = self.request("POST", "/api/query/nope", {"params": {}})
        self.assertEqual(res.status, 404)
        self.assertIn("error", json.loads(body))
        res, body = self.request("GET", "/api/nothing-here")
        self.assertEqual(res.status, 404)

    def test_no_path_reaches_the_filesystem(self):
        for path in ("/../pyproject.toml", "/static/app.js", "/app.js/../../x",
                     "/%2e%2e/rsse/cli.py"):
            with self.subTest(path=path):
                res, _ = self.request("GET", path)
                self.assertEqual(res.status, 404)


if __name__ == "__main__":
    unittest.main()
