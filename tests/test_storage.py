from concurrent.futures import ThreadPoolExecutor

from reactor_agent.domain.models import ReactorModelSpec


def test_spec_versions_are_persisted_without_mutating_prior(database):
    spec = ReactorModelSpec(user_request="first")
    database.create_task(spec)
    revised = spec.next_version(user_request="updated")
    database.save_spec(revised)
    assert database.get_spec(spec.task_id).version == 2
    assert database.get_spec(spec.task_id, 1).user_request == "first"
    assert database.get_spec(spec.task_id, 2).parent_version == 1


def test_queue_claim_is_atomic_across_workers(database):
    spec = ReactorModelSpec(user_request="queue")
    database.create_task(spec)
    queue_id = database.enqueue(spec.task_id, {"version": 1})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: database.claim_next(), range(2)))
    claimed = [item for item in results if item]
    assert len(claimed) == 1
    assert claimed[0]["queue_id"] == queue_id
    assert claimed[0]["attempts"] == 1


def test_wal_mode_is_enabled(database):
    with database.connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_new_task_does_not_inherit_prior_parameters(database):
    first = ReactorModelSpec(user_request="old", known_parameters={"conversion": 0.5})
    second = ReactorModelSpec(user_request="new")
    database.create_task(first)
    database.create_task(second)
    assert database.get_spec(second.task_id).known_parameters.conversion is None


def test_interrupted_queue_items_can_be_recovered(database):
    spec = ReactorModelSpec(user_request="recover")
    database.create_task(spec)
    database.enqueue(spec.task_id, {"version": 1})
    item = database.claim_next()
    assert item
    assert database.recover_interrupted_queue_items() == 1
    assert database.claim_next()["queue_id"] == item["queue_id"]



def test_queue_claim_can_target_one_run_and_duplicate_submission_is_idempotent(database):
    spec = ReactorModelSpec(user_request="specific queue")
    database.create_task(spec)
    first = database.enqueue(spec.task_id, {"spec_version": 1})
    assert database.enqueue(spec.task_id, {"spec_version": 1}) == first
    second = database.enqueue(spec.task_id, {"spec_version": 2})
    assert database.claim_next(second)["queue_id"] == second
    assert database.claim_next()["queue_id"] == first


def _claim_in_spawned_process(database_path):
    from reactor_agent.storage.db import Database
    item = Database(database_path).claim_next()
    return item["queue_id"] if item else None


def test_queue_claim_is_atomic_across_processes(database):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    spec = ReactorModelSpec(user_request="cross process")
    database.create_task(spec)
    queue_id = database.enqueue(spec.task_id, {"spec_version": 1})
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_claim_in_spawned_process, [str(database.path)] * 2))
    assert results.count(queue_id) == 1
    assert results.count(None) == 1
