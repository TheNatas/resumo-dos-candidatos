"""Shared helpers for the ALESC collectors: id prefixing and name-based matching.

🚨 **ALESC exposes no numeric member id and no CPF.** `Mandate.house_member_id` is the
WordPress **profile slug** (e.g. ``ana-campagnolo``) — the only stable handle the
institution publishes. Every other ALESC source (expense CSV `Conta`, extrato de
votação, presença table, proposition `Autoria`) identifies a deputy by a *display
name*, so joining them to a mandate is unavoidably **name-based**. That is why
:class:`MandateIndex` exists, why it is deliberately tolerant, and why every miss is
logged instead of dropped in silence.

🚨 **ALESC publishes no CPF and no birth date for state deputies.** `Person.cpf`
therefore stays ``None`` for ALESC-seeded people and downstream entity resolution to
TSE candidacies is name-based (never `cpf_exact`). Do not "fill in" a CPF here.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import House, Mandate
from resumo.util import normalize_name

logger = logging.getLogger("resumo.ingestion.alesc")

# e-Legis ids are opaque hashids ("57x9N", "524wm") — short, alphanumeric, NOT
# sequential and NOT guessable, so the indexes must be crawled to enumerate them.
# They are prefixed before storage because `proposition.proposition_id`,
# `vote.id_votacao` and `attendance_record.id_evento` are shared with Câmara/Senado
# (whose ids are bare integers): the prefix makes collision impossible by construction.
ID_PREFIX = "AL"

# Honorifics and Portuguese name particles carry no discriminating power.
#
# The academic titles matter because ALESC's own sources abbreviate them differently
# for the same person: the roster says "Profª Vanessa da Rosa" and the e-Legis
# iniciativa <select> says "Deputada Prof. Vanessa da Rosa". Normalization strips the
# ordinal indicator to PROFA and the period to PROF, so the two spellings stop being
# the same token and the deputy silently loses her match. Dropping the title entirely
# makes both read as {VANESSA, ROSA}.
#
# Only titles anyone may hold are listed. Occupational nicknames that ALESC treats as
# part of the name (DELEGADO, SARGENTO, PADRE) are NOT noise: for several deputies
# they are the most distinctive token there is, and discarding them would collapse
# two different people onto one match.
_NOISE = frozenset(
    {
        "DEPUTADO", "DEPUTADA", "DEP", "DA", "DE", "DI", "DO", "DAS", "DOS", "E",
        "PROF", "PROFA", "DR", "DRA", "SR", "SRA",
    }
)
# Minimum rapidfuzz score for a last-resort fuzzy hit, and the margin the winner must
# beat the runner-up by. Deliberately strict: a wrong attribution here would put one
# deputy's expenses on another's public record.
_FUZZY_FLOOR = 88.0
_FUZZY_MARGIN = 6.0


def proposition_id(elegis_hash: str) -> str:
    """``KMmqv`` -> ``ALKMmqv`` (see :data:`ID_PREFIX`)."""
    return f"{ID_PREFIX}{elegis_hash}"


def votacao_id(extrato_hash: str) -> str:
    return f"{ID_PREFIX}{extrato_hash}"


def evento_id(session_hash: str) -> str:
    return f"{ID_PREFIX}{session_hash}"


def slug_to_name(slug: str) -> str:
    return slug.replace("-", " ").strip()


def _tokens(normalized: str) -> frozenset[str]:
    return frozenset(t for t in normalized.split() if t not in _NOISE and len(t) > 1)


def name_variants(raw: str | None) -> list[str]:
    """Normalized spellings worth trying for one display name.

    ``"Ana Paula da Silva (Paulinha)"`` -> the whole string, the part before the
    parenthesis, and the nickname inside it — ALESC's own sources disagree on which
    of the three they print.
    """
    if not raw:
        return []
    out: list[str] = []
    full = normalize_name(raw)
    if full:
        out.append(full)
    if "(" in raw:
        head = normalize_name(raw.split("(", 1)[0])
        inner = normalize_name(raw.split("(", 1)[1].split(")", 1)[0])
        for candidate in (head, inner):
            if candidate and candidate not in out:
                out.append(candidate)
    return out


@dataclass(frozen=True)
class MandateRef:
    slug: str
    mandate_id: uuid.UUID | None
    nome: str | None
    # Nomes civis conhecidos do mesmo deputado. O portal da transparência publica o
    # nome civil ("CARLOS HENRIQUE DE LIMA") e o e-Legis o parlamentar ("Sargento
    # Lima"): sem essa ponte, o gasto fica sem dono. O nome civil vem do arquivo do
    # próprio TSE, não de palpite.
    aliases: tuple[str, ...] = ()


@dataclass
class MandateIndex:
    """Resolve an ALESC display name to a mandate. Tolerant, but never silent."""

    refs: list[MandateRef]
    unmatched: Counter[str] = field(default_factory=Counter)
    _exact: dict[str, MandateRef] = field(default_factory=dict, repr=False)
    _ambiguous: set[str] = field(default_factory=set, repr=False)
    _cache: dict[str, MandateRef | None] = field(default_factory=dict, repr=False)
    _by_tokens: list[tuple[frozenset[str], MandateRef]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        for ref in self.refs:
            for raw in (ref.nome, slug_to_name(ref.slug), *ref.aliases):
                for variant in name_variants(raw):
                    existing = self._exact.get(variant)
                    if existing is not None and existing.slug != ref.slug:
                        self._ambiguous.add(variant)
                    self._exact.setdefault(variant, ref)
        self._by_tokens = [
            (_tokens(variant), ref)
            for variant, ref in self._exact.items()
            if variant not in self._ambiguous and len(_tokens(variant)) >= 2
        ]

    def __len__(self) -> int:
        return len(self.refs)

    def by_slug(self, slug: str) -> MandateRef | None:
        return next((r for r in self.refs if r.slug == slug), None)

    def match(self, raw: str | None, *, record_unmatched: bool = True) -> MandateRef | None:
        """Exact normalized hit -> token-subset hit -> strict fuzzy hit -> None.

        `record_unmatched=False` when the caller is *probing* a vocabulary rather than
        placing rows — matching e-Legis's ~280 ``iniciativa`` options against a 61-seat
        roster legitimately misses ~220 times (comissões, Poder Executivo, legislaturas
        passadas), and letting those into :attr:`unmatched` would bury the misses that
        actually cost data under noise that never had a mandate to find.
        """
        key = (raw or "").strip()
        if not key:
            return None
        if key in self._cache:
            hit = self._cache[key]
            if hit is None and record_unmatched:
                self.unmatched[key] += 1
            return hit
        hit = self._resolve(key)
        self._cache[key] = hit
        if hit is None and record_unmatched:
            self.unmatched[key] += 1
        return hit

    def _resolve(self, raw: str) -> MandateRef | None:
        variants = name_variants(raw)
        for variant in variants:
            if variant in self._exact and variant not in self._ambiguous:
                return self._exact[variant]
        if not variants:
            return None

        # Token subset: the roster prints "Ana Campagnolo" while the expense CSV prints
        # the civil name "Ana Caroline Campagnolo". A roster name whose tokens are all
        # present in the incoming name is a match — but only if exactly one is.
        for variant in variants:
            incoming = _tokens(variant)
            if len(incoming) < 2:
                continue
            subset = {ref.slug: ref for tokens, ref in self._by_tokens if tokens <= incoming}
            if len(subset) == 1:
                return next(iter(subset.values()))

        # Last resort: strict fuzzy, and only when the winner is clearly ahead.
        scored: list[tuple[float, MandateRef]] = []
        for indexed, ref in self._exact.items():
            if indexed in self._ambiguous:
                continue
            best = max(fuzz.token_set_ratio(variant, indexed) for variant in variants)
            scored.append((best, ref))
        if not scored:
            return None
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top_score, top_ref = scored[0]
        runner_up = next((s for s, r in scored if r.slug != top_ref.slug), 0.0)
        if top_score >= _FUZZY_FLOOR and top_score - runner_up >= _FUZZY_MARGIN:
            return top_ref
        return None

    def report_unmatched(self, collector: str, source: str) -> str | None:
        """Log every unmatched name once with its row count. Returns a short summary.

        Unmatched rows are skipped (the FK/NOT NULL columns need a member id), but they
        are never dropped quietly — a name we cannot place is usually a suplente who
        assumed office, i.e. a roster gap worth fixing.
        """
        if not self.unmatched:
            return None
        total = sum(self.unmatched.values())
        for name, count in self.unmatched.most_common():
            logger.warning(
                "%s: %r (%s row(s) in %s) matched no ALESC mandate — skipped",
                collector, name, count, source,
            )
        return f"{total} row(s) across {len(self.unmatched)} unmatched name(s)"


def mandate_map(session: Session, id_legislatura: int) -> dict[str, uuid.UUID]:
    """{house_member_id (profile slug) -> mandate_id} for one ALESC legislature."""
    rows = session.execute(
        select(Mandate.house_member_id, Mandate.id).where(
            Mandate.house == House.ASSEMBLEIA, Mandate.id_legislatura == id_legislatura
        )
    )
    return {slug: mid for slug, mid in rows}


def mandate_index(session: Session, id_legislatura: int) -> MandateIndex:
    """Índice de nomes -> mandato, incluindo o nome civil que o TSE publica.

    A ALESC fala de si mesma com dois vocabulários: o e-Legis usa o nome
    parlamentar, o portal da transparência usa o nome civil. O arquivo do TSE
    conhece os dois para quem se elegeu, então é ele que costura um ao outro — em
    vez de deixar o gasto sem dono ou, pior, atribuí-lo por semelhança.
    """
    from resumo.resolution.bridge import recover_cpfs

    rows = list(
        session.execute(
            select(Mandate.house_member_id, Mandate.id, Mandate.nome_parlamentar).where(
                Mandate.house == House.ASSEMBLEIA, Mandate.id_legislatura == id_legislatura
            )
        )
    )
    # Falha de ponte não pode derrubar a coleta de despesas: sem alias, o índice
    # volta a ser exatamente o de antes.
    try:
        bridged = recover_cpfs(session, before_year=get_settings().election_year)
    except Exception:  # noqa: BLE001
        logger.warning("mandate_index: ponte de identidade indisponível; seguindo sem aliases")
        bridged = {}

    refs = []
    for slug, mid, nome in rows:
        ident = bridged.get(str(mid))
        aliases = (ident.source_nome,) if ident and ident.source_nome else ()
        refs.append(MandateRef(slug, mid, nome, aliases))
    return MandateIndex(refs)
