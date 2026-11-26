from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.db import Base
import app.models  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
ALEMBIC_INI = BACKEND / "alembic.ini"


def run_alembic(database_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *arguments],
        cwd=BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        shell=False,
    )


def test_alembic_offline_and_online_upgrade_head(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-smoke.db"

    offline = run_alembic(database_path, "upgrade", "head", "--sql")
    assert offline.returncode == 0, offline.stderr
    assert "CREATE TABLE projects" in offline.stdout
    assert "CREATE TABLE generation_jobs" in offline.stdout
    assert not database_path.exists(), "offline migration must not touch the database"

    online = run_alembic(database_path, "upgrade", "head")
    assert online.returncode == 0, online.stderr
    assert database_path.exists()

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert tables == set(Base.metadata.tables) | {"alembic_version"}
        assert {'quality_profiles','quality_renders','local_models','model_experiments'} <= tables
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
    finally:
        engine.dispose()

    second_upgrade = run_alembic(database_path, "upgrade", "head")
    assert second_upgrade.returncode == 0, second_upgrade.stderr


def test_timeout_migration_clears_existing_service_caps(tmp_path: Path) -> None:
    database_path = tmp_path / "timeout-migration.db"
    upgraded = run_alembic(database_path, "upgrade", "0011")
    assert upgraded.returncode == 0, upgraded.stderr

    from sqlalchemy.orm import Session
    from app.models import AIService
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with Session(engine) as session:
            session.add(AIService(module="video_generation", name="legacy profile", provider="comfyui",
                                  base_url="http://127.0.0.1:8189", settings_json={"timeout": 600, "concurrency": 1}))
            session.commit()
    finally:
        engine.dispose()


def test_project_reference_video_link_migration_backfills_active_shot_choices(tmp_path: Path) -> None:
    database_path = tmp_path / "project-reference-video-links.db"
    upgraded = run_alembic(database_path, "upgrade", "0013")
    assert upgraded.returncode == 0, upgraded.stderr

    from sqlalchemy.orm import Session
    from app.models import Project, ReferenceVideo, ShotDetail, StoryboardShot

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with Session(engine) as session:
            first_project = Project(id="prj_migration_first", name="first")
            second_project = Project(id="prj_migration_second", name="second")
            first_shot = StoryboardShot(id="shot_migration_first", project_id=first_project.id, order=1)
            second_shot = StoryboardShot(id="shot_migration_second", project_id=second_project.id, order=1)
            active_video = ReferenceVideo(
                id="refvideo_active", name="active", filename="active.mp4",
                relative_path="reference/active.mp4", sha256="a" * 64, state="active",
            )
            inactive_video = ReferenceVideo(
                id="refvideo_inactive", name="inactive", filename="inactive.mp4",
                relative_path="reference/inactive.mp4", sha256="b" * 64, state="deleted",
            )
            session.add_all([
                first_project, second_project, first_shot, second_shot, active_video, inactive_video,
                ShotDetail(
                    shot_id=first_shot.id,
                    source_json={"production": {"motion_references": [
                        {"video_id": active_video.id}, {"video_id": inactive_video.id}, {"video_id": "missing"},
                    ]}},
                ),
                ShotDetail(
                    shot_id=second_shot.id,
                    source_json={"production": {"motion_references": [{"video_id": active_video.id}]}},
                ),
            ])
            session.commit()
    finally:
        engine.dispose()

    upgraded = run_alembic(database_path, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            links = set(connection.execute(text(
                "SELECT project_id, video_id FROM project_reference_videos"
            )).tuples())
            assert links == {
                ("prj_migration_first", "refvideo_active"),
                ("prj_migration_second", "refvideo_active"),
            }
    finally:
        engine.dispose()

    upgraded = run_alembic(database_path, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with Session(engine) as session:
            service = session.query(AIService).filter_by(name="legacy profile").one()
            assert service.settings_json == {"timeout": None, "concurrency": 1}
    finally:
        engine.dispose()

