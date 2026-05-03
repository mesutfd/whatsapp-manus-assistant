"""
Smart Contact Resolution - Fuzzy Name Matching Engine.

Solves the problem: User says "send to Masoud Nayebi" but the contact is saved as
"Masoud Nayebi-Tech Assistant". This module finds the best match using multiple
fuzzy matching strategies.
"""

import logging
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    """Normalize a name for comparison (lowercase, strip special chars)."""
    if not name:
        return ""
    # Lowercase
    name = name.lower().strip()
    # Remove common suffixes/prefixes that are metadata, not name parts
    # Keep them for matching but also try without them
    return name


def tokenize_name(name: str) -> List[str]:
    """Split a name into meaningful tokens."""
    if not name:
        return []
    # Split on spaces, hyphens, underscores, dots
    tokens = re.split(r'[\s\-_\.]+', name.lower().strip())
    # Remove empty tokens
    return [t for t in tokens if t]


def compute_similarity(query: str, candidate: str) -> float:
    """
    Compute a composite similarity score between a query name and a candidate contact name.
    Returns a score between 0.0 and 1.0.

    Uses multiple strategies:
    1. Exact substring match (highest priority)
    2. Token overlap (all query tokens found in candidate)
    3. SequenceMatcher ratio (general fuzzy match)
    4. Starts-with bonus
    """
    if not query or not candidate:
        return 0.0

    query_norm = normalize_name(query)
    candidate_norm = normalize_name(candidate)

    # Strategy 1: Exact match
    if query_norm == candidate_norm:
        return 1.0

    # Strategy 2: Query is a complete substring of candidate
    # e.g., "Masoud Nayebi" is inside "Masoud Nayebi-Tech Assistant"
    if query_norm in candidate_norm:
        # Score based on how much of the candidate the query covers
        coverage = len(query_norm) / len(candidate_norm)
        return 0.85 + (coverage * 0.15)  # 0.85 to 1.0

    # Strategy 3: Token-based matching
    query_tokens = tokenize_name(query)
    candidate_tokens = tokenize_name(candidate)

    if query_tokens and candidate_tokens:
        # Count how many query tokens appear in candidate tokens
        matched_tokens = 0
        for qt in query_tokens:
            for ct in candidate_tokens:
                if qt in ct or ct in qt:
                    matched_tokens += 1
                    break
                # Also check high similarity between tokens
                if SequenceMatcher(None, qt, ct).ratio() > 0.8:
                    matched_tokens += 1
                    break

        token_score = matched_tokens / len(query_tokens)

        # If ALL query tokens match, this is very likely the right contact
        if matched_tokens == len(query_tokens):
            return 0.80 + (token_score * 0.15)

    # Strategy 4: General sequence matching
    seq_ratio = SequenceMatcher(None, query_norm, candidate_norm).ratio()

    # Strategy 5: Starts-with bonus
    starts_bonus = 0.0
    if candidate_norm.startswith(query_norm[:3]) if len(query_norm) >= 3 else False:
        starts_bonus = 0.05

    # Combine strategies
    # Token score is weighted higher than raw sequence ratio
    token_weight = 0.6
    seq_weight = 0.4

    if query_tokens and candidate_tokens:
        token_score_val = (matched_tokens / len(query_tokens)) if query_tokens else 0
        combined = (token_score_val * token_weight) + (seq_ratio * seq_weight) + starts_bonus
    else:
        combined = seq_ratio + starts_bonus

    return min(combined, 1.0)


def resolve_contact(
    query: str,
    contacts: List[Dict[str, Any]],
    threshold: float = 0.5,
    max_results: int = 5,
) -> List[Dict[str, Any]]:
    """
    Resolve a contact name query against a list of contacts using fuzzy matching.

    Args:
        query: The name/identifier the user provided (e.g., "Masoud Nayebi")
        contacts: List of contact dicts with 'name', 'jid', 'phone' fields
        threshold: Minimum similarity score to include in results (0.0 to 1.0)
        max_results: Maximum number of results to return

    Returns:
        List of matched contacts sorted by relevance, each with a 'match_score' field.
    """
    if not query or not contacts:
        return []

    scored_contacts = []

    for contact in contacts:
        # Try matching against all name fields
        name = contact.get("name", "") or ""
        phone = contact.get("phone", "") or ""
        jid = contact.get("jid", "") or ""

        # Compute scores against different fields
        name_score = compute_similarity(query, name)

        # Also check if query looks like a phone number
        phone_score = 0.0
        query_digits = re.sub(r'\D', '', query)
        if query_digits and len(query_digits) >= 5:
            # Phone number matching
            if query_digits in phone or query_digits in jid:
                phone_score = 0.95

        # Take the best score
        best_score = max(name_score, phone_score)

        if best_score >= threshold:
            result = {**contact, "match_score": round(best_score, 4)}
            scored_contacts.append(result)

    # Sort by score descending
    scored_contacts.sort(key=lambda x: x["match_score"], reverse=True)

    return scored_contacts[:max_results]


def resolve_single_contact(
    query: str,
    contacts: List[Dict[str, Any]],
    threshold: float = 0.5,
) -> Optional[Dict[str, Any]]:
    """
    Resolve to the single best matching contact.
    Returns None if no match above threshold is found.
    """
    results = resolve_contact(query, contacts, threshold, max_results=1)
    return results[0] if results else None
