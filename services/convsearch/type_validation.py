"""Product-type validation: principal product vs incidental mention.

The hard requirement of the migration: a candidate qualifies as the
REQUESTED product only when the evidence shows the listing IS that product,
not an accessory, spare part, or an unrelated product that merely mentions
the same words. The rules are category-general — no per-product keyword
patch:

1.  Type-token coverage and head-noun consistency are read from the title.
    The candidate's own identity must carry the requested product type; an
    explicit accessory title such as «пружина для катушки» still fails even
    if its description repeats the host product.
2.  Generic bundle titles (a set/kit) are not automatically the requested
    item, but they may be sent for semantic review when the description
    names the requested type.
3.  Description-only type evidence never creates an automatic match. It only
    opens the candidate for bounded semantic review, where title and
    description are considered together.
4.  Ambiguous words are resolved by collocation. «Катушка» alone may mean
    a fishing reel or an audio/line spool; without the request's context
    tokens the candidate is not accepted automatically.
5.  A type mentioned only in a later comma/semicolon/pipe-separated title
    fragment is also sent for semantic review, never treated as a verified
    principal-item match.

Everything here is deterministic and unit-testable; the engine consumes it
in ``services.convsearch.ranking``/``engine.shape_pool``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from models.product import Product

from services.convsearch.text import tokens

#: Accessory / spare-part markers that make a title's OWN head noun the
#: accessory, not the product. These are generic role words (not product
#: names): «держатель», «пружина», «запчасть», «ремкомплект», «футляр»…
#: A title that LEADS with one of these and mentions the type only after
#: «для …» is an accessory FOR the product, never the product itself.
_GENERIC_BUNDLE_HEADS: frozenset[str] = frozenset(
    {"набор", "комплект", "set", "kit", "bundle", "pack"}
)

ACCESSORY_HEAD_WORDS: frozenset[str] = frozenset(
    {
        # RU generic accessory/repair heads
        "держатель", "подставка", "крепление", "кронштейн", "чехол",
        "футляр", "сумка", "кейс", "коробка", "подставка",
        "пружина", "шестерн", "шестерён", "подшипник", "подшипники",
        "винт", "винты", "болт", "гайка", "кронштейн", "ремкомплект", "кабель", "провод",
        "запчасть", "запчасти", "ремонт", "набор", "комплект", "детал",
        "замен", "сменный", "сменная", "сменные", "сменное",
        "запасн", "запасная", "запасные", "фитинг", "адаптер",
        "переходник", "насадка", "насадки", "бита", "биты", "сверл",
        "диск", "диски", "лезвие", "лезвия", "нож", "ножи",
        "катушкодер", "шпуля", "шпули", "барабан",
        # EN generic accessory/repair heads
        "holder", "stand", "mount", "bracket", "case", "cover", "bag",
        "spring", "gear", "gears", "bearing", "bearings", "screw",
        "screws", "repair", "kit", "spare", "replacement", "replace",
        "adapter", "mount", "bit", "bits", "blade", "blades", "knife",
        "spool", "reel cover", "carrier",
    }
)

#: Role prepositions that mark an accessory relationship ("X для Y" — X is
#: an accessory FOR Y). The requested type appearing AFTER one of these is
#: an incidental mention, not the product's own identity.
_ROLE_PREPS: tuple[str, ...] = ("для", "for", "к", "to")

#: Common RU adjective endings; a token ending with one qualifies as an
#: adjective (e.g. «рыболовная»), which in a title usually modifies the
#: NEXT noun (the head noun).
_ADJ_ENDINGS: tuple[str, ...] = (
    "ый", "ий", "ой", "ая", "яя", "ое", "ые", "ие", "ем", "ым", "им",
    "ого", "его", "ому", "ему", "ую", "юю",
)


def stems_match(a: str, b: str) -> bool:
    """Whether two Russian/English word forms are the SAME word.

    Russian inflection («шуруповерт» / «шуруповерта» / «шуруповёрты»)
    must never break product-type matching, so tokens compare by their
    shared stem: a 4+ char common prefix (the project's own `_stem`
    convention), falling back to exact equality for short words.
    """

    a = a.lower().replace("ё", "е")
    b = b.lower().replace("ё", "е")
    if a == b:
        return True
    stem_len = min(4, len(a), len(b))
    if stem_len < 4:
        return False
    return a[:stem_len] == b[:stem_len]


@dataclass
class TypeVerdict:
    """One candidate's product-type verdict with the reasons it was decided."""

    product_id: str
    qualifies: bool
    score: float
    #: Human-readable reasons (log only): "type_head", "accessory_head:держатель"…
    reasons: list[str] = field(default_factory=list)


def is_accessory_product_type(product_type: str | None) -> bool:
    """Whether the requested product identity is a generic accessory/fitment.

    This role-level check lets the conversation state distinguish «X на Y»
    compatibility (an attachment for a drill, a case for a phone) from a
    soft activity context («подарок на свадьбу»). It uses generic product
    roles, never individual product names or brands.
    """

    type_tokens = tokens(product_type or "")
    non_fitment_set_words = ("набор", "комплект", "set", "kit")
    return any(
        stems_match(token, accessory)
        for token in type_tokens
        if not any(stems_match(token, generic) for generic in non_fitment_set_words)
        for accessory in ACCESSORY_HEAD_WORDS
    )


def _head_noun(type_tokens: list[str]) -> str | None:
    """The type's distinguishing head noun — the LAST content token.

    «рыболовная катушка» → «катушка»; «повербанк» → «повербанк». English
    types keep their last token the same way ("power bank" → "bank",
    handled by token coverage anyway).
    """

    return type_tokens[-1] if type_tokens else None


def _is_adjective(token: str) -> bool:
    return token.endswith(_ADJ_ENDINGS)


def _find_role_split(title_tokens: list[str]) -> int | None:
    """Index of the first role preposition («для»/«for»), else None.

    Everything BEFORE it is the title's own product identity; everything
    AFTER it is what that product serves or fits.
    """

    for index, token in enumerate(title_tokens):
        if token in _ROLE_PREPS:
            return index
    return None


def _raw_tokens(text: str) -> list[str]:
    """Lowercase tokens WITH stopwords kept (role prepositions survive)."""

    return [
        t.lower().replace("ё", "е")
        for t in re.findall(r"[^\W\d_][^\W_]*|\d+", text or "")
    ]


def validate_product_type(product: Product, product_type: str | None) -> TypeVerdict:
    """Whether ``product`` IS the requested ``product_type`` (hard gate).

    Deterministic, evidence-based, category-general. A candidate passes
    only when its own identity (the part of the title BEFORE any «для …»
    role clause) carries the type's tokens; an accessory whose title
    merely references the type inside a role clause fails.
    """

    title_tokens = _raw_tokens(product.title or "")
    description_tokens = _raw_tokens(product.description or "")
    type_tokens = tokens(product_type or "")

    reasons: list[str] = []
    if not type_tokens:
        # No explicit type requested: nothing to enforce — the engine's own
        # relevance stage still gates quality.
        return TypeVerdict(
            product_id=product.product_id or "", qualifies=True, score=1.0,
            reasons=["no_type_requested"],
        )
    if not title_tokens:
        return TypeVerdict(
            product_id=product.product_id or "", qualifies=False, score=0.0,
            reasons=["empty_title"],
        )

    head = _head_noun(type_tokens)
    # The candidate's OWN identity is everything before a role preposition:
    # "держатель для рыболовной катушки" → identity = ["держатель"].
    split = _find_role_split(title_tokens)
    own_tokens = title_tokens[:split] if split is not None else title_tokens

    def _contains(tokens_: list[str], needle: str) -> bool:
        return any(stems_match(token, needle) for token in tokens_)

    # 1. The candidate's own identity must carry at least ONE type token
    #    (the principal noun or its qualifier). "Защитный ремешок для умных
    #    часов" carries «ремешок» in its own identity even though the type
    #    tail «часов» sits after the role clause — that is a watch strap,
    #    the very product requested.
    any_type_in_own = any(
        _contains(own_tokens, token) for token in type_tokens
    )
    # The requested distinguishing noun is stronger identity evidence than
    # a shared modifier. Keep the broader signal separately for ambiguous
    # candidates that need semantic review.
    head_ok = head is not None and _contains(own_tokens, head)
    # A head that appears ONLY after the role preposition (with nothing of
    # the type in the own identity) is an accessory reference:
    # «пружина для катушки» mentions «катушка» but is not one.
    head_after_role = (
        not any_type_in_own
        and head is not None
        and split is not None
        and _contains(title_tokens[split + 1:], head)
    )

    # A multi-word product name can be mentioned in the genitive as a
    # modifier of a DIFFERENT product: «переключатель сварочного аппарата».
    # Token coverage alone incorrectly accepts this because both requested
    # words occur in the title. When the requested qualifier appears in a
    # genitive adjective form after an unrelated noun, it is not evidence
    # that the listing itself is the requested product.
    genitive_mention = False
    if len(type_tokens) > 1:
        qualifier = type_tokens[0]
        qualifier_positions = [
            i for i, token in enumerate(title_tokens)
            if stems_match(token, qualifier)
        ]
        if qualifier_positions:
            pos = qualifier_positions[0]
            if pos > 0:
                observed = title_tokens[pos]
                previous = title_tokens[pos - 1]
                genitive_adjective = observed.endswith(("ого", "его", "ой", "ей"))
                previous_is_adjective = _is_adjective(previous)
                genitive_mention = (
                    genitive_adjective
                    and not previous_is_adjective
                    and not stems_match(previous, qualifier)
                )

    # 2. Accessory head word leads the title: the listing IS the accessory
    #    ("держатель катушки" — the holder, not the reel). The requested
    #    head noun appearing AFTER the accessory head within the own
    #    identity is a genitive accessory relation, never the product.
    accessory_head = next(
        (token for token in own_tokens[:2] if token.startswith(tuple(ACCESSORY_HEAD_WORDS))),
        None,
    )
    accessory_composite = (
        accessory_head is not None
        and head is not None
        and any(
            stems_match(token, head)
            for token in own_tokens[
                own_tokens.index(accessory_head) + 1:
            ]
        )
    )

    # A type mentioned only in a later comma-separated listing fragment is
    # not strong evidence that the listing's principal item IS that type.
    # AliExpress titles often concatenate several unrelated products; for
    # example, a title may begin with a drill and mention attachments only in
    # a later list. Do not accept that incidental mention as a deterministic
    # identity match. Keep it eligible for the bounded semantic review, where
    # the full listing and active intent can be considered; if review is
    # unavailable, the engine quarantines it rather than showing it blindly.
    primary_clause = re.split(r"[,;|]", product.title or "", maxsplit=1)[0]
    primary_tokens = _raw_tokens(primary_clause)
    head_in_primary = head is not None and _contains(primary_tokens, head)
    late_type_mention = (
        head_ok
        and not head_in_primary
        and any(_contains(title_tokens, token) for token in type_tokens)
    )

    # 3. Token coverage over the whole title (fallback evidence).
    matched = sum(1 for token in type_tokens if _contains(title_tokens, token))
    coverage = matched / len(type_tokens)

    # 4. Ambiguity guard: a bare type noun with NO context tokens (no
    #    adjective like «рыболовная», no distinguishing qualifier) carries
    #    insufficient evidence — "катушка" alone can be an audio tape reel
    #    as easily as a fishing reel. Reject honesty-first.
    # 4. Ambiguity guard: the title carries the head noun but NONE of the
    #    type's qualifier tokens («рыболовная катушка» requested, title says
    #    «кассетная катушка»): the words alone describe a DIFFERENT product
    #    of the same noun family. Reject honesty-first — the words do not
    #    establish the requested subtype.
    bare_noun_only = (
        len(type_tokens) > 1
        and head is not None
        and _contains(title_tokens, head)
        and not any(
            token != head and _contains(title_tokens, token)
            for token in type_tokens
        )
    )

    qualifies = True
    score = coverage

    if head_after_role:
        qualifies = False
        reasons.append("type_only_in_role_clause")
    if genitive_mention:
        qualifies = False
        reasons.append("type_mentioned_as_genitive_modifier")
    semantic_review_required = False
    description_has_type = any(
        _contains(description_tokens, token) for token in type_tokens
    )
    if accessory_head is not None and (not any_type_in_own or accessory_composite):
        is_generic_bundle = any(
            stems_match(accessory_head, bundle_head)
            for bundle_head in _GENERIC_BUNDLE_HEADS
        )
        if is_generic_bundle and description_has_type and not head_after_role:
            # A bundle title can be broad while its description identifies
            # the actual contents. It is not an automatic match; defer it.
            semantic_review_required = True
            score = 0.15
            reasons.append("type_identity_requires_semantic_review")
        else:
            qualifies = False
            reasons.append(f"accessory_head:{accessory_head}")
    if bare_noun_only:
        qualifies = False
        reasons.append("ambiguous_bare_noun")
    if late_type_mention and qualifies:
        semantic_review_required = True
        score = 0.15
        reasons.append("type_mentioned_only_after_listing_separator")
    if not head_ok and not head_after_role:
        # A shared modifier alone does not prove product identity:
        # «сварочный стол» is not necessarily a «сварочный аппарат».
        # But it also does not prove the candidate is unrelated: valid
        # product variants may use a different head noun (e.g. «сварочный
        # полуавтомат»). Keep candidates carrying a type token in their OWN
        # identity for semantic review, never as deterministic matches.
        if semantic_review_required:
            pass
        elif any_type_in_own:
            semantic_review_required = True
            score = 0.15
            reasons.append("type_identity_requires_semantic_review")
        elif description_has_type:
            # AliExpress titles can be generic while the description names
            # the actual item. Description evidence is never enough for an
            # automatic identity pass: it only opens the candidate for the
            # bounded semantic review, where title and description are read
            # together. An explicit title naming another product still wins.
            semantic_review_required = True
            score = 0.15
            reasons.append("type_identity_requires_semantic_review")
        elif coverage < 1.0:
            qualifies = False
            reasons.append("type_head_missing")
        else:
            reasons.append("type_reordered")
    if coverage < 1.0 and qualifies:
        # Partial coverage still accepted (title carries the head plus
        # context), but scored lower than an exact head match.
        score = 0.5 * coverage
        reasons.append("partial_type_coverage")

    if qualifies and head_ok and not semantic_review_required:
        score = 1.0 + 0.5 * (coverage - 1.0) if coverage < 1.0 else 1.0
        reasons.append("type_head")

    return TypeVerdict(
        product_id=product.product_id or "",
        qualifies=qualifies,
        score=score,
        reasons=reasons or ["passed"],
    )
