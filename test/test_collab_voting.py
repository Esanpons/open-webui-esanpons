"""Tests de la votació de consens (voting._vote_on_proposal).

Cobreix el bug MR-01: el proposant no ha de votar la seva pròpia proposta, ni
tan sols quan té un display_name (àlies) diferent del nom del model — cas en
què la comparació antiga per nom fallava i el proposant s'auto-votava.

També cobreix MR-04: el vot es llegeix de l'últim bloc JSON vàlid, no del
primer match textual dins d'un raonament.

Segueix el patró de la resta de tests d'orquestrador: `def test_...` amb un
`async def scenario()` intern executat amb `asyncio.run` (no depenem de la
config asyncio_mode de pytest).
"""

import asyncio
import importlib.metadata
from types import SimpleNamespace

_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

from open_webui.collab import voting
from open_webui.collab import orchestrator
from open_webui.collab.config import CollabConfig


def _setup(monkeypatch, *, down=None, vote_contents, asked):
    """Prepara els mocks de _vote_on_proposal.

    vote_contents: dict agent_id -> string que retornaria el model per aquell
    votant (o None si "no respon"). `asked` acumula qui és consultat.
    """

    async def fake_quick_completion(request, user, channel, config, agent_id, system, prompt, task):
        asked.append(agent_id)
        return vote_contents.get(agent_id)

    async def fake_transcript(*_a, **_kw):
        return "(transcripció)"

    async def fake_board(*_a, **_kw):
        return ""

    async def fake_down(*_a, **_kw):
        return down or {}

    # _quick_completion s'importa dins de la funció (from orchestrator import),
    # així que el patch ha d'anar sobre el mòdul orchestrator d'on prové.
    monkeypatch.setattr(orchestrator, "_quick_completion", fake_quick_completion)
    monkeypatch.setattr(voting, "build_transcript", fake_transcript)
    monkeypatch.setattr(voting, "_board_text", fake_board)
    monkeypatch.setattr(voting, "get_down_agents", fake_down)


def _config(agents):
    return CollabConfig(enabled=True, agents=agents)


def _channel():
    return SimpleNamespace(id="ch1")


def test_proposer_with_display_name_does_not_vote(monkeypatch):
    """El proposant identificat per by_id no entra a la llista de votants,
    encara que el seu display_name no coincideixi amb el nom del model."""

    async def scenario():
        agents = ["a1", "a2"]
        models = {"a1": {"name": "Model A"}, "a2": {"name": "Model B"}}
        # a1 proposa; el seu display_name (àlies) és "Aleix", ≠ "Model A".
        proposal = {"by": "Aleix", "by_id": "a1", "summary": "fet", "kind": "finish"}
        # Si a1 votés, votaria a favor; a2 vota en contra → sense a1, guanya "no".
        asked = []
        _setup(
            monkeypatch,
            asked=asked,
            vote_contents={"a1": '{"agree": true}', "a2": '{"agree": false}'},
        )
        consensus, agrees, disagrees = await voting._vote_on_proposal(
            None, _channel(), _config(agents), None, models, proposal
        )
        assert "a1" not in asked, "el proposant no s'ha de consultar"
        assert asked == ["a2"]
        assert (agrees, disagrees) == (0, 1)
        assert consensus is False

    asyncio.run(scenario())


def test_solo_proposer_gives_automatic_consensus(monkeypatch):
    async def scenario():
        agents = ["a1"]
        models = {"a1": {"name": "Model A"}}
        proposal = {"by": "Aleix", "by_id": "a1", "summary": "fet", "kind": "finish"}
        asked = []
        _setup(monkeypatch, asked=asked, vote_contents={})
        consensus, agrees, disagrees = await voting._vote_on_proposal(
            None, _channel(), _config(agents), None, models, proposal
        )
        assert consensus is True
        assert (agrees, disagrees) == (0, 0)

    asyncio.run(scenario())


def test_legacy_proposal_without_by_id_falls_back_to_name(monkeypatch):
    """Propostes antigues (sense by_id) segueixen excloent per nom del model."""

    async def scenario():
        agents = ["a1", "a2"]
        models = {"a1": {"name": "Model A"}, "a2": {"name": "Model B"}}
        proposal = {"by": "Model A", "summary": "fet", "kind": "finish"}  # sense by_id
        asked = []
        _setup(
            monkeypatch,
            asked=asked,
            vote_contents={"a1": '{"agree": true}', "a2": '{"agree": true}'},
        )
        consensus, agrees, disagrees = await voting._vote_on_proposal(
            None, _channel(), _config(agents), None, models, proposal
        )
        assert "a1" not in asked
        assert consensus is True
        assert (agrees, disagrees) == (1, 0)

    asyncio.run(scenario())


def test_down_agents_do_not_vote(monkeypatch):
    async def scenario():
        agents = ["a1", "a2", "a3"]
        models = {a: {"name": a.upper()} for a in agents}
        proposal = {"by": "A1", "by_id": "a1", "summary": "fet", "kind": "finish"}
        asked = []
        _setup(
            monkeypatch,
            asked=asked,
            down={"a2": {"reason": "timeout", "since": 0}},
            vote_contents={"a3": '{"agree": true}'},
        )
        consensus, agrees, disagrees = await voting._vote_on_proposal(
            None, _channel(), _config(agents), None, models, proposal
        )
        assert asked == ["a3"], "ni el proposant (a1) ni el caigut (a2) voten"
        assert consensus is True

    asyncio.run(scenario())


def test_vote_reads_last_json_block(monkeypatch):
    """Si el model raona amb un JSON preliminar 'agree: false' i conclou amb
    'agree: true', compta la conclusió (últim bloc), no la primera."""

    async def scenario():
        agents = ["a1", "a2"]
        models = {"a1": {"name": "A1"}, "a2": {"name": "A2"}}
        proposal = {"by": "A1", "by_id": "a1", "summary": "fet", "kind": "finish"}
        reasoning = (
            'Primer pensava {"agree": false, "reason": "dubte"} però ho he revisat.\n'
            'Conclusió: {"agree": true, "reason": "està complet"}'
        )
        asked = []
        _setup(monkeypatch, asked=asked, vote_contents={"a2": reasoning})
        consensus, agrees, disagrees = await voting._vote_on_proposal(
            None, _channel(), _config(agents), None, models, proposal
        )
        assert (agrees, disagrees) == (1, 0)
        assert consensus is True

    asyncio.run(scenario())


def test_tie_is_not_consensus(monkeypatch):
    async def scenario():
        agents = ["a1", "a2", "a3"]
        models = {a: {"name": a.upper()} for a in agents}
        proposal = {"by": "A1", "by_id": "a1", "summary": "fet", "kind": "finish"}
        asked = []
        _setup(
            monkeypatch,
            asked=asked,
            vote_contents={"a2": '{"agree": true}', "a3": '{"agree": false}'},
        )
        consensus, agrees, disagrees = await voting._vote_on_proposal(
            None, _channel(), _config(agents), None, models, proposal
        )
        assert (agrees, disagrees) == (1, 1)
        assert consensus is False  # empat = NO consens

    asyncio.run(scenario())
