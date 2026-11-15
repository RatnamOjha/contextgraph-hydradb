import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend import app
from backend.router import route_graph_candidates


class FakeGraphDB:
    def __init__(self, weights=(1.0,)):
        self.weights = list(weights)
        self.updates = {}
        self.last_query = ""
        self.queries = []

    @staticmethod
    def id_for(kind, key):
        return abs(hash((kind, key))) % (2**31)

    async def query(self, query, params=None):
        self.last_query = query
        self.queries.append(query)
        if "RETURN r.weight AS weight" in query:
            return ["weight"], [[weight] for weight in self.weights], 0.1
        if "SET r.weight =" in query:
            weight = float(re.search(r"SET r\.weight = ([0-9.eE+-]+)", query).group(1))
            self.weights = [weight for _ in self.weights]
            return [], [], 0.1
        if "MERGE (u {id: row.id})" in query:
            for row in params["rows"]:
                self.updates[row["id"]] = {
                    "tool": row["tool"], "dependency": row["dependency"], "previous_weight": row.get("previous_weight"),
                    "applied_weight": row.get("applied_weight"), "pre_state": row["pre_state"],
                    "experiment_state": row["experiment_state"], "edge_identifier": row["edge_identifier"],
                    "state": row["state"], "evidence": row["evidence"], "relationship_type": row["relationship_type"],
                    "original_task": row["original_task"], "graph_version": row["graph_version"],
                    "proposed_graph_correction": row["proposed_graph_correction"]}
            return [], [], 0.1
        if "RETURN u.tool AS tool,u.dependency AS dependency,u.pre_state AS pre_state" in query:
            u = self.updates[params["id"]]
            return ["tool", "dependency", "pre_state", "experiment_state", "state", "original_task", "graph_version", "proposed_graph_correction"], [[
                u["tool"], u["dependency"], u["pre_state"], u["experiment_state"], u["state"], u["original_task"],
                u["graph_version"], u["proposed_graph_correction"]]], 0.1
        if "RETURN u.state AS state,u.evidence AS evidence" in query:
            u = self.updates[params["id"]]
            return ["state", "evidence", "diagnosis", "post_state", "rollback_state"], [[
                u.get("state"), u.get("evidence"), u.get("diagnosis"), u.get("post_state"), u.get("rollback_state")]], 0.1
        if "RETURN u.state AS state,u.rollback_state AS rollback_state" in query:
            u = self.updates[params["id"]]
            return ["state", "rollback_state", "evidence"], [[u.get("state"), u.get("rollback_state"), u.get("evidence")]], 0.1
        if "RETURN u.id AS id" in query:
            rows = [[ident, row["tool"], row["dependency"], row["pre_state"], row["experiment_state"]]
                    for ident, row in self.updates.items() if row.get("state") == "experimenting"]
            return ["id", "tool", "dependency", "pre_state", "experiment_state"], rows, 0.1
        if "RETURN u.tool AS tool" in query:
            update_id = int(re.search(r"id: (\d+)", query).group(1))
            u = self.updates[update_id]
            return ["tool", "dependency", "previous", "applied", "pre", "post", "state", "edge", "evidence"], [[
                u["tool"], u["dependency"], u["previous_weight"], u["applied_weight"], u["pre_state"],
                u.get("post_state"), u["state"], u["edge_identifier"], u.get("evidence")]], 0.1
        if "SET u.state = $state" in query:
            update_id = int(re.search(r"id: (\d+)", query).group(1))
            self.updates[update_id].update(params)
            return [], [], 0.1
        raise AssertionError("Unexpected graph query: " + query)


class DependencyStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        self.state_patch = patch.object(app, "_learning_state_dir", Path(self.state_dir.name))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    async def test_withhold_restore_and_rollback_preserve_existing_relationship(self):
        fake = FakeGraphDB()
        with patch("backend.app.db", fake):
            baseline = await app.get_dependency_state("order.items", "order.details")
            update_id = 1001
            await app.create_learning_experiment(update_id, "order.items", "order.details", "fixture",
                "train", "heldout", baseline)
            experiment = json.loads(fake.updates[update_id]["experiment_state"])
            correction = await app.get_dependency_state("order.items", "order.details")
            await app.finish_learning_experiment(update_id, state="applied", evidence={"validated": True},
                diagnosis="test", post_state=correction)
            fake.updates[update_id]["post_state"] = json.dumps(correction)
            rolled = await app.rollback_learning(update_id)
        self.assertEqual(baseline, {"edge_exists": True, "weight": 1.0, "properties": {"weight": 1.0}})
        self.assertEqual(experiment["weight"], 1.0)
        self.assertEqual(experiment["retrieval_exclusion"], {"from": "order.items", "relationship_type": "DEPENDS_ON", "to": "order.details"})
        self.assertEqual(correction, baseline)
        self.assertEqual(rolled["state"], "rolled_back")
        self.assertEqual(rolled["restored_state"], baseline)
        self.assertEqual(fake.updates[update_id]["original_task"], "train")
        self.assertEqual(fake.updates[update_id]["graph_version"], app._read_learning_state(update_id)["graph_version"])
        self.assertIn('"actual_execution_failure"', fake.updates[update_id]["evidence"])
        self.assertEqual(app._read_learning_state(update_id)["evidence_store"], "HydraDB_readback_confirmed")
        create_query = next(query for query in fake.queries if "MERGE (u {id: row.id})" in query)
        self.assertIn("u.state=row.state", create_query)
        self.assertIn("u.evidence=row.evidence", create_query)
        self.assertNotIn("u.state='experimenting'", create_query)
        self.assertEqual(fake.weights, [1.0])
        self.assertEqual(len(fake.weights), 1)
        self.assertFalse(any("SET r.weight" in q for q in fake.queries))
        self.assertFalse(rolled["edge_written"])
        self.assertEqual(fake.updates[update_id]["state"], "rolled_back")
        self.assertEqual(rolled["evidence_store"], "HydraDB_readback_confirmed")

    async def test_missing_or_duplicated_edge_fails_closed(self):
        with patch("backend.app.db", FakeGraphDB(weights=())):
            with self.assertRaises(HTTPException) as missing:
                await app.get_dependency_state("order.items", "order.details")
        self.assertEqual(missing.exception.status_code, 409)
        with patch("backend.app.db", FakeGraphDB(weights=(1.0, 1.0))):
            with self.assertRaises(HTTPException) as duplicate:
                await app.get_dependency_state("order.items", "order.details")
        self.assertEqual(duplicate.exception.status_code, 409)

    async def test_weightless_edge_is_distinct_from_missing_relationship(self):
        fake = FakeGraphDB(weights=(None,))
        with patch("backend.app.db", fake):
            state = await app.get_dependency_state("order.items", "order.details")
        self.assertEqual(state, {"edge_exists": True, "weight": None, "weight_state": "absent_or_null", "properties": {}})
        self.assertNotIn("weight", state["properties"])
        self.assertNotIn("SET r.weight", "\n".join(fake.queries))

    async def test_weightless_edge_snapshot_persists_and_rollback_only_verifies_it(self):
        fake = FakeGraphDB(weights=(None,))
        with patch("backend.app.db", fake):
            baseline = await app.get_dependency_state("order.items", "order.details")
            await app.create_learning_experiment(1011, "order.items", "order.details", "fixture", "train", "heldout", baseline)
            record = app._read_learning_state(1011)
            self.assertEqual(record["pre_state"], baseline)
            create_query = next(query for query in fake.queries if "MERGE (u {id: row.id})" in query)
            self.assertNotIn("row.previous_weight", create_query)
            self.assertNotIn("row.applied_weight", create_query)
            self.assertNotIn("u.previous_weight=row.previous_weight", create_query)
            self.assertNotIn("u.applied_weight=row.applied_weight", create_query)
            await app.finish_learning_experiment(1011, state="applied", evidence={"validated": True},
                diagnosis="retrieval_filter_removed", post_state=baseline, rollback_state=baseline,
                rollback_verification={"verified": True, "edge_written": False})
            result = await app.rollback_learning(1011)
        self.assertEqual(result["state"], "rolled_back")
        self.assertIsNone(result["restored_weight"])
        self.assertEqual(result["restored_state"], baseline)
        self.assertFalse(result["edge_written"])
        self.assertFalse(any("SET r.weight" in query for query in fake.queries))

    async def test_weightless_interrupted_recovery_verifies_without_edge_write(self):
        fake = FakeGraphDB(weights=(None,))
        with patch("backend.app.db", fake):
            baseline = await app.get_dependency_state("order.items", "order.details")
            await app.create_learning_experiment(1012, "order.items", "order.details", "fixture", "train", "heldout", baseline)
            await app.recover_interrupted_learning_experiments()
            restored = await app.get_dependency_state("order.items", "order.details")
        self.assertEqual(restored, baseline)
        self.assertEqual(fake.updates[1012]["state"], "interrupted_restored")
        self.assertFalse(any("SET r.weight" in query for query in fake.queries))

    async def test_fixture_endpoint_records_weightless_retrieval_sequence_and_rollback(self):
        fake = FakeGraphDB(weights=(None,))
        baseline = {"edge_exists": True, "weight": None, "weight_state": "absent_or_null", "properties": {}}
        def route(withheld=False):
            tools = [{"key": "order.items"}]
            edges = [{"from": "product_research", "to": "order.items", "type": "USES"}]
            if not withheld:
                tools.append({"key": "order.details"})
                edges.append({"from": "order.items", "to": "order.details", "type": "DEPENDS_ON"})
            return {"tools": tools, "edges": edges, "resolved_entities": [], "retrieval_ms": 1.0}
        routes = [route(), route(withheld=True), route()]
        executions = [
            {"success": False, "answer": "blocked", "tool_calls": 1, "tool_outputs": [], "trace": [],
                "dependency_failures": [{"tool": "order.items", "missing_dependency": "order.details"}]},
            {"success": True, "answer": "verified", "tool_calls": 2, "tool_outputs": [], "trace": [],
                "dependency_failures": []},
        ]
        with patch("backend.app.db", fake), patch("backend.app.olist_available", return_value=True), \
             patch("backend.app.list_orders", return_value=[{"order_id": "train"}, {"order_id": "heldout"}]), \
             patch("backend.app.route_graph_candidates", side_effect=routes), \
             patch("backend.app.execute_deterministic", side_effect=executions), \
             patch("backend.app.record_learning_execution", new=AsyncMock()), \
             patch("backend.app._learning_state_dir", Path(self.state_dir.name)):
            result = await app.learning_demo()
        self.assertEqual(result["state"], "applied")
        self.assertEqual(result["graph_states"]["baseline"], baseline)
        self.assertEqual(result["graph_states"]["rollback"], baseline)
        self.assertTrue(result["rollback_verification"]["verified"])
        self.assertFalse(result["rollback_verification"]["edge_written"])
        record = app._read_learning_state(result["update_id"])
        timeline = record["evidence_record"]["learning_sequence"]
        self.assertEqual([row["stage"] for row in timeline], [
            "baseline", "dependency_withheld_from_retrieval", "validated_correction", "heldout_retest", "rollback_verification"])
        self.assertFalse(any("SET r.weight" in query for query in fake.queries))

    async def test_learning_sidecar_falls_back_when_configured_directory_is_read_only(self):
        blocked = Path(self.state_dir.name) / "read-only"
        fallback = Path(self.state_dir.name) / "writable-temp"
        original_mkdir = Path.mkdir
        def mkdir(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError("simulated Render read-only app directory")
            return original_mkdir(path, *args, **kwargs)
        with patch.object(app, "_learning_state_dir", blocked), \
             patch("backend.app.Path.mkdir", new=mkdir), \
             patch("backend.app.tempfile.gettempdir", return_value=str(fallback)):
            app._write_learning_state(1013, {"id": 1013, "state": "experimenting"})
            recovered = app._read_learning_state(1013)
        self.assertEqual(recovered, {"id": 1013, "state": "experimenting"})
        self.assertTrue((fallback / "contextgraph-learning-updates" / "1013.json").is_file())

    async def test_startup_recovers_an_interrupted_withholding_experiment(self):
        fake = FakeGraphDB()
        with patch("backend.app.db", fake):
            baseline = await app.get_dependency_state("order.items", "order.details")
            await app.create_learning_experiment(1003, "order.items", "order.details", "real_groq",
                "train", "heldout", baseline)
            await app.recover_interrupted_learning_experiments()
            restored = await app.get_dependency_state("order.items", "order.details")
        self.assertEqual(restored, baseline)
        self.assertEqual(fake.updates[1003]["state"], "interrupted_restored")

    async def test_rollback_refuses_to_overwrite_intervening_graph_change(self):
        fake = FakeGraphDB(weights=(0.5,))
        fake.updates[1002] = {"tool": "order.items", "dependency": "order.details", "previous_weight": 0.25,
            "applied_weight": 0.75, "pre_state": json.dumps({"edge_exists": True, "weight": 0.25, "properties": {"weight": 0.25}}),
            "post_state": json.dumps({"edge_exists": True, "weight": 0.75, "properties": {"weight": 0.75}}),
            "edge_identifier": "logical", "state": "applied"}
        with patch("backend.app.db", fake):
            with self.assertRaises(HTTPException) as caught:
                await app.rollback_learning(1002)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(fake.weights, [0.5])
        self.assertEqual(fake.updates[1002]["state"], "applied")

    async def test_retrieval_filter_withholds_only_the_requested_dependency(self):
        class RouteDB:
            def __init__(self): self.queries = []
            @staticmethod
            def id_for(kind, key): return 1 if key == "order.items" else 2
            async def query(self, query, params=None):
                self.queries.append(query)
                if "OPTIONAL MATCH (t)-[:PROVIDED_BY]" in query:
                    return [], [["product_research", 1, "order.items", "Items", "List items", "olist", "{}", None]], 0.1
                if "MATCH (a:Tool)-[r:DEPENDS_ON]->(b:Tool)" in query:
                    if "AND NOT (a.key = 'order.items' AND b.key = 'order.details')" in query:
                        return [], [], 0.1
                    if "a.id IN [1]" in query:
                        return [], [[1, "order.items", 2, "order.details", "Details", "Get details", "olist", "{}", None]], 0.1
                    return [], [], 0.1
                raise AssertionError(query)

        db = RouteDB()
        candidates = [{"skill": "product_research", "score": 1, "evidence": ["items"]}]
        normal = await route_graph_candidates(db, candidates)
        withheld = await route_graph_candidates(db, candidates, exclude_dependencies={("order.items", "order.details")})
        self.assertEqual({row["key"] for row in normal["tools"]}, {"order.items", "order.details"})
        self.assertEqual({row["key"] for row in withheld["tools"]}, {"order.items"})
        self.assertTrue(all(query.lstrip().startswith("MATCH") for query in db.queries))

    async def test_learning_record_falls_back_and_rolls_back_without_edge_writes(self):
        class ReadOnlyDB:
            def __init__(self): self.queries = []
            @staticmethod
            def id_for(kind, key): return 1 if key == "order.items" else 2
            async def query(self, query, params=None):
                self.queries.append(query)
                if "RETURN r.weight AS weight" in query:
                    return ["weight"], [[1.0]], 0.1
                raise RuntimeError("writer not available")

        db = ReadOnlyDB()
        with patch("backend.app.db", db):
            baseline = await app.get_dependency_state("order.items", "order.details")
            await app.create_learning_experiment(1010, "order.items", "order.details", "fixture", "train", "heldout", baseline)
            await app.finish_learning_experiment(1010, state="applied", evidence={"validated": True},
                diagnosis="retrieval_filter_removed", post_state=baseline)
            result = await app.rollback_learning(1010)
        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(result["evidence_store"], "local_sidecar")
        self.assertEqual(result["restored_state"], baseline)
        self.assertFalse(any("SET r.weight" in query for query in db.queries))


if __name__ == "__main__":
    unittest.main()
