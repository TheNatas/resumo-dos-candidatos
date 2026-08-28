"""Executive-branch collectors — the sitting governor's mandate and its record.

Every other collector in this package reads a *legislature*, and the schema grew up
around that shape: a member id, a legislatura, roll-call votes, attendance, a cota
parlamentar. None of it fits an executive, and the absence was not cosmetic — with no
`Mandate` row a sitting governor could only be described as "no accepted link", which
the public UI renders as the absence of the "tentando reeleição" badge. A reader
cannot tell that apart from "not an incumbent".

Two collectors, deliberately split by what they read:

* :mod:`~resumo.ingestion.executivo.governadores` — **who holds the office**, derived
  from the TSE result the platform already ingested. No network.
* :mod:`~resumo.ingestion.executivo.atos` — **what they did with it**, scraped from
  the ALESC e-Legis under ``iniciativa=governador-do-estado``.

They are split because the first is a legal fact with a definitive federal source and
the second is a scrape of a state portal that can drift. Folding them together would
let a markup change upstream take out the incumbency flag, which is the more important
of the two claims and the one with the sturdier source.
"""

from __future__ import annotations
