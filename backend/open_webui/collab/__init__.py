"""Espai de treball col·laboratiu multi-agent ("taula rodona d'IAs").

Mòdul propi del fork (vegeu docs/plans/espai-collaboratiu.md). Tot el codi
nou viu aquí; els únics punts de contacte amb el nucli d'Open WebUI estan
marcats amb el comentari `# [collab-fork]`.
"""

from open_webui.collab.orchestrator import handle_collab_message

__all__ = ["handle_collab_message"]
