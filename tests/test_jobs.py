"""Jobs keep separate work apart: memory, files and history never mix."""

import time

from fastapi.testclient import TestClient

from nathan import jobs
from nathan.memory import Memory
from nathan.web.app import create_app

H = {"x-nathan": "1"}


def test_memory_recall_stays_inside_each_job(tmp_path):
    mem = Memory(tmp_path / "m.sqlite")
    chem, trip = {"id": "chem1", "name": "Chem labs"}, {"id": "trip1", "name": "Lisbon"}
    for job, fact in ((chem, "the titration prelab uses phenolphthalein"), (trip, "the titration museum tour is Friday"),
                      (None, "general titration note")):
        token = jobs.current.set(job)
        mem.remember(fact)
        jobs.current.reset(token)
    seen = {}
    for job in (chem, trip, None):
        token = jobs.current.set(job)
        seen[job and job["id"]] = [e.content for e in mem.search("titration")]
        jobs.current.reset(token)
    assert seen == {"chem1": ["the titration prelab uses phenolphthalein"],
                    "trip1": ["the titration museum tour is Friday"],
                    None: ["general titration note"]}


def test_each_job_gets_its_own_files_folder(tmp_path):
    assert jobs.workspace(tmp_path) == tmp_path
    token = jobs.current.set({"id": "abcd1234", "name": "CHEM 1111: Labs!"})
    try:
        assert jobs.workspace(tmp_path) == tmp_path / "jobs" / "chem-1111-labs-abcd"
    finally:
        jobs.current.reset(token)


def test_jobs_api_and_runs_inside_a_job(make_engine):
    asked = []

    def responder(ref, messages, **kw):
        asked.append(messages[-1]["content"])
        return "done"

    client = TestClient(create_app(make_engine(responder)))
    with client:
        job = client.post("/api/jobs", json={"name": "Chem labs", "notes": "CHEM 1111, Dr. Lee"}, headers=H).json()
        assert client.post("/api/jobs", json={"name": " "}, headers=H).status_code == 400
        run = client.post("/api/runs", json={"prompt": "summarize prelab 4", "mode": "single", "job": job["id"]},
                          headers=H).json()
        for _ in range(100):
            if client.app.state.runs.get(run["id"]).finished:
                break
            time.sleep(0.05)
        assert any("Chem labs" in a and "Dr. Lee" in a for a in asked)
        [listed] = client.get("/api/jobs").json()["jobs"]
        assert listed["asks"] == 1
        assert "summarize prelab 4" in client.get(f"/api/jobs/{job['id']}/history").json()["items"][0]["text"]
        client.post(f"/api/jobs/{job['id']}", json={"archived": True}, headers=H)
        assert client.get("/api/jobs").json()["jobs"] == []
        assert client.post("/api/runs", json={"prompt": "x", "job": "gone"}, headers=H).status_code == 404
