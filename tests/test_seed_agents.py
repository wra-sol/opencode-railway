import os

import seed_agents as sa

AGENTS_REL = os.path.join(".config", "opencode", "AGENTS.md")
SIDECAR_REL = os.path.join(".config", "opencode", ".AGENTS.md.bundled.sha256")


def _agents(p):
    return os.path.join(p, AGENTS_REL)


def _sidecar(p):
    return os.path.join(p, SIDECAR_REL)


def _sha(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_seeds_when_absent(tmp_path):
    assert sa.seed(str(tmp_path)) == "seeded"
    assert os.path.isfile(_agents(tmp_path))
    assert open(_agents(tmp_path)).read() == sa.BUNDLED_AGENTS_MD
    assert open(_sidecar(tmp_path)).read().strip() == _sha(sa.BUNDLED_AGENTS_MD)


def test_noop_when_current(tmp_path):
    assert sa.seed(str(tmp_path)) == "seeded"
    # Second call must be a no-op and must not change mtime-worthy content.
    assert sa.seed(str(tmp_path)) == "current"
    assert open(_agents(tmp_path)).read() == sa.BUNDLED_AGENTS_MD


def test_upgrades_pristine_prior_bundled(tmp_path):
    # Synthesize a fake "previous bundled" version + write its hash to the
    # sidecar, simulating an existing deploy seeded by an older image.
    old = "# opencode Railway server\n\nOLD PREVIOUS BUNDLED VERSION\n"
    os.makedirs(os.path.dirname(_agents(tmp_path)), exist_ok=True)
    open(_agents(tmp_path), "w").write(old)
    open(_sidecar(tmp_path), "w").write(_sha(old) + "\n")

    assert sa.seed(str(tmp_path)) == "upgraded"
    assert open(_agents(tmp_path)).read() == sa.BUNDLED_AGENTS_MD
    assert open(_sidecar(tmp_path)).read().strip() == _sha(sa.BUNDLED_AGENTS_MD)


def test_preserves_user_edits(tmp_path):
    assert sa.seed(str(tmp_path)) == "seeded"
    user = sa.BUNDLED_AGENTS_MD + "\n## My custom section\nedited by user\n"
    open(_agents(tmp_path), "w").write(user)  # disk != bundled, disk != sidecar

    assert sa.seed(str(tmp_path)) == "preserved"
    assert open(_agents(tmp_path)).read() == user
    # sidecar must not be advanced to claim the edited file is bundled-pristine
    assert open(_sidecar(tmp_path)).read().strip() == _sha(sa.BUNDLED_AGENTS_MD)


def test_preserves_when_sidecar_missing_and_disk_differs(tmp_path):
    os.makedirs(os.path.dirname(_agents(tmp_path)), exist_ok=True)
    open(_agents(tmp_path), "w").write("something else entirely\n")
    # no sidecar -> can't prove it was pristine -> preserve (safe default)

    assert sa.seed(str(tmp_path)) == "preserved"
    assert open(_agents(tmp_path)).read() == "something else entirely\n"
    assert not os.path.isfile(_sidecar(tmp_path))


def test_prep_calls_seed_agents_module():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prep.sh")).read()
    assert "seed_agents.py" in src
    assert "python3 /seed_agents.py --data" in src
