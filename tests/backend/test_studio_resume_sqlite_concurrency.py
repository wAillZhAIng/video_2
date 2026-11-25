"""Actual SQLite lock contention; no provider calls or model/media output."""
import asyncio
import copy

from fastapi import Request
import pytest
from sqlalchemy import func,select

from app import models as m
from app.api import studio
from app.core.errors import AppError
from test_reference_budget_repair import setup_shot


def test_resume_does_not_block_async_writer_that_must_release_sqlite(client,settings):
    case=setup_shot(client,settings,run_status='paused')
    with client.app.state.session_factory() as check:
        original=copy.deepcopy(check.get(m.StudioRun,case.run_id).nodes_json)

    async def scenario():
        with client.app.state.session_factory() as writer,client.app.state.session_factory() as controller:
            controller.connection().exec_driver_sql('PRAGMA busy_timeout=300')
            writer.add(m.AuditEvent(object_type='unit_concurrent_project',object_id='other-project',
                action='actual_sqlite_writer',details_json={'provider_calls':0}))
            writer.flush()  # An actual independent SQLite transaction owns the writer lock.
            async def release_after_network_yield():
                await asyncio.sleep(.05)
                writer.commit()
            releaser=asyncio.create_task(release_after_network_yield())
            try:
                await asyncio.wait_for(studio.resume(case.project_id,
                    Request({'type':'http','app':client.app}),controller),2)
                await releaser
            finally:
                if not releaser.done():
                    releaser.cancel()
                    with pytest.raises(asyncio.CancelledError):await releaser
    asyncio.run(scenario())
    with client.app.state.session_factory() as check:
        run=check.get(m.StudioRun,case.run_id)
        assert run.status=='queued' and run.nodes_json==original
        assert check.scalar(select(func.count()).select_from(m.GenerationJob))==0
        assert check.scalar(select(func.count()).select_from(m.AuditEvent).where(
            m.AuditEvent.action=='actual_sqlite_writer'))==1


def test_two_resumes_keep_same_project_serialization_without_duplicate_jobs(client,settings):
    case=setup_shot(client,settings,run_status='paused')
    async def scenario():
        with client.app.state.session_factory() as first,client.app.state.session_factory() as second:
            responses=await asyncio.gather(*[studio.resume(case.project_id,
                Request({'type':'http','app':client.app}),session) for session in [first,second]],
                return_exceptions=True)
            errors=[item for item in responses if isinstance(item,Exception)]
            assert len(errors)==1 and isinstance(errors[0],AppError)
            assert errors[0].code=='studio_not_paused'
    asyncio.run(scenario())
    with client.app.state.session_factory() as check:
        assert check.get(m.StudioRun,case.run_id).status=='queued'
        assert check.scalar(select(func.count()).select_from(m.GenerationJob))==0
