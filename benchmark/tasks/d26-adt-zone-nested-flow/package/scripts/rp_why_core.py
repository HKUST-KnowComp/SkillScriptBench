"""
rp-why Core: Shared constants, classifiers, and zone computation.

This module is the single source of truth for:
- DOK classification patterns and logic
- Orchestra Tier (TM) definitions
- ADT diagnostic zone computation
- Compression detection
- System message filtering
- Growth nudges by zone

Imported by: rp_why_baseline.py, growth_nudge.py, goose_skill.py
"""
from __future__ import annotations
import re
from typing import List, Tuple
DOK_PATTERNS: dict = {1: ['\\bhow do i\\b', '\\bwhat is\\b', '\\bsyntax\\b', '\\bcommand\\b', '\\bwhere\\b', '\\bshow me\\b', '\\bexample\\b', '\\bhelp with\\b', "\\bwhat\\'s the\\b", '\\bhow to\\b', '\\bcan you show\\b', '\\blist\\b', '\\bdefine\\b', '\\blook up\\b', '\\blook at\\b', '\\btake a look\\b', '\\bfind the\\b', '\\bread the\\b', '\\bopen the\\b', '\\bget the\\b', '\\bcheck on\\b', '\\bdisplay\\b', '\\bpull up\\b', '\\bwhat does .+ (do|mean|say)\\b', '\\bremind me\\b', '\\bwhere is\\b', '\\bwhere are\\b'], 2: ['\\bimplement\\b', '\\bdebug\\b', '\\bfix\\b', '\\brefactor\\b', '\\btest\\b', '\\badd\\b', '\\bupdate\\b', '\\bcreate\\b', '\\bbuild\\b', '\\bwrite\\b', '\\bmodify\\b', '\\bchange\\b', '\\bremove\\b', '\\bdelete\\b', '\\binstall\\b', '\\bsetup\\b', '\\bconfigure\\b', '\\bmigrate\\b', '\\bconvert\\b', '\\brun\\b', '\\bexecute\\b', '\\bdeploy\\b', '\\bformat\\b', '\\brename\\b', '\\bmove\\b', '\\bcopy\\b', '\\bmerge\\b', '\\bcommit\\b', '\\bpush\\b', '\\brebase\\b'], 3: ['\\bdesign\\b', '\\barchitect\\b', '\\banalyze\\b', '\\bcompare\\b', '\\btrade-?off\\b', '\\bbest approach\\b', '\\bevaluate\\b', '\\bstrategy\\b', '(?<![-\\w])why\\b', '\\bimplications\\b', '\\bshould we\\b', '\\breview\\b', '\\boptimize\\b', '\\bimprove\\b', '\\balternative\\b', '\\bpros and cons\\b', '\\bdecision\\b', '\\bplan\\b', '\\bwhat if\\b', '\\bconsequences\\b', '\\bprioritize\\b', '\\bvalidat\\w*\\b', '\\bverif\\w*\\b', '\\bensur\\w*\\b', '\\bsound\\b', '\\binteract\\w*\\b', '\\bassumption\\w*\\b', '\\bwhat would break\\b', '\\bedge case\\w*\\b', '\\bhold\\w* up\\b', '\\bthink through\\b', '\\breason\\w* about\\b', '\\bweigh\\b', '\\bconsider\\b', '\\bdepend\\w* on\\b', '\\bmake sure\\b', '\\bensure that\\b', '\\bshould be\\b', '\\bsupposed to\\b', '\\binstead of\\b', '\\brather than\\b', '\\bappropriate\\b', '\\bcorrect\\w*\\b', '\\balign\\w*\\b', '\\bdistinction\\b', '\\bdifference between\\b', '\\brelat\\w+ to\\b', '\\binterplay\\b', '\\bobserv\\w*\\b', '\\bglean\\b', '\\bsignifican\\w*\\b', '\\bmethodology\\b', '\\bapproach\\b', '\\bhow does .+ (relate|connect|fit|work with)\\b', '\\bdiagnos\\w*\\b', '\\bassess\\w*\\b', '\\bmeasur\\w*\\b', '\\bcriteria\\b', '\\brequirement\\w*\\b'], 4: ['\\bresearch\\b', '\\bnovel\\b', '\\binnovate\\b', '\\btransform\\b', '\\bframework\\b', '\\blong-term\\b', '\\bevolve\\b', '\\bvision\\b', '\\bbreakthrough\\b', '\\bsystematic\\b', '\\bparadigm\\b', '\\bfundamental\\b', '\\bpioneer\\b', '\\bgroundbreaking\\b', '\\bsynthesize\\b', '\\bcross-disciplinary\\b', '\\boriginal\\b', '\\bnew model\\b', '\\btheory\\b', '\\bhypothesis\\b', '\\bpropos\\w*\\b', '\\bpublish\\w*\\b', '\\bcontribut\\w*\\b', '\\bextend\\b', '\\bexpand upon\\b', '\\bintegrat\\w*\\b', '\\bformulat\\w*\\b', '\\bconceptualiz\\w*\\b', '\\bcatalog\\w*\\b', '\\btaxonom\\w*\\b', '\\bclassif\\w*\\b']}
TM_TIERS: dict = {1: 'Solo', 2: 'Duet', 3: 'Ensemble', 4: 'Chamber', 5: 'Symphony', 6: 'Virtuoso'}
DOK_NAMES: dict = {1: 'Recall & Reproduction', 2: 'Application of Skills & Concepts', 3: 'Strategic Thinking', 4: 'Extended Thinking'}
DOK_NAMES_SHORT: dict = {1: 'Recall', 2: 'Application', 3: 'Strategic', 4: 'Extended'}
ADT_ZONES: list = ['Overpowered', 'Underutilizing', 'Expected', 'Growing', 'Frontier', 'Thinking Ahead']
ZONE_COLORS: dict = {'Expected': 'grey', 'Growing': 'blue', 'Frontier': 'green', 'Thinking Ahead': 'purple', 'Underutilizing': 'amber', 'Overpowered': 'red'}
SYSTEM_MESSAGE_PATTERNS: list = ['^A (Python|shell|bash|TypeScript) (script|command|execution) was (executed|attempted|run|performed)', '^Retrieved (the|a|lines) .+ (file|document|contents|data|from|interface|class|method)', '^The (assistant|user|developer|tool|search|grep|build|shell) (retrieved|executed|ran|displayed|checked|searched|found|confirmed|examined|updated|added|created|removed)', '^A (file edit|directory tree|shell command|delegation request|background task|code edit|git|grep|search|build|new|import)', '^(File|Directory) (was|tree|listing)', '^A cached (HTML|file|document)', '^A (git|grep|code|recursive|file|Gradle|TypeScript|GitHub|Linear) .+ was (executed|performed|made|run|attempted|retrieved|created|updated)', '^(An|The) (import|code|method|function|dependency|constructor|test|build|emulator|Android|app)', '^(Checked|Verified|Examined|Updated|Added|Removed|Switched|Pushed|Committed)', '^(Both|All|Two|Three|The existing|The new|The legacy|The Compose)', '^(Good|Done|Clean|Build) [-.] ', '^A (new|Compose|Material|Kotlin|Java) .+ (was|file|class|interface)', '^(Found|Confirmed|Resolved|Deployed|Installed|Launched|Untracked)', '^A (dependency injection|DI|Dagger|Anvil) .+ (was|field|binding)', '^(Re-review|CI|Unit test|Snapshot|Detekt|ktfmt)']
COMPRESSION_MAX_WORDS: int = 8
COMPRESSION_INDICATORS: list = ['^proceed$', '^do it$', '^go$', '^yes$', '^continue$', '^ship it$', '^deploy$', '^merge$', '^lgtm$', '^next$', '^done$', '^send$', '^push$', '^run it$', '^build$', '^start$', '^finish$']
ZONE_NUDGES: dict = {'Frontier': ['Operating at the productive edge. Document what works for others.', 'The collaboration is matched. Look for opportunities to extend into new domains.', 'Strong session. Consider extending one thread into a multi-session investigation.'], 'Growing': ['Approaching a match. Keep pushing DOK 3+ work and the zone will shift.', "Consider: what's one workflow you could delegate more fully?", 'Building momentum. Try framing one more task as a design decision rather than an execution request.'], 'Expected': ["Healthy starting position. Growth comes from asking 'why' before implementing.", 'Try framing one task as a design decision rather than an execution request.', "Solid foundation. Ask 'what are the trade-offs?' before your next implementation prompt."], 'Thinking Ahead': ['Cognitive depth exceeds tool sophistication. Time to adopt more powerful orchestration.', 'Your thinking is ready for the next tier. Explore sub-agents or multi-step delegation.', 'What tool or workflow would unlock the depth you are already thinking at?'], 'Underutilizing': ['Powerful tools deserve powerful questions. Before each prompt: can this be more strategic?', 'Batch simple queries. Reserve the agent for work that requires reasoning.', 'What is the most strategic question you could ask right now?'], 'Overpowered': ['Significant mismatch. Consider whether this task needs an autonomous agent.', 'Opportunity: redirect this tool toward a problem that requires analysis or design.', 'Is there a harder problem this tool should be pointed at?']}
ZONE_REFLECTIONS: dict = {'Frontier': 'What complex challenge could benefit from sustained exploration across your next few sessions?', 'Growing': 'What workflow could you delegate more fully to the agent?', 'Expected': 'What strategic question have you been avoiding?', 'Thinking Ahead': 'What tool or workflow would unlock the depth you are already thinking at?', 'Underutilizing': 'What is the most strategic question you could ask right now?', 'Overpowered': 'Is there a harder problem this tool should be pointed at?'}

def is_system_message(text: str) -> bool:
    """Detect system-generated messages (tool output summaries).

    These are NOT human prompts and should be excluded from DOK
    classification entirely.
    """
    if not text:
        return False
    for pattern in SYSTEM_MESSAGE_PATTERNS:
        if re.match(pattern, text, re.IGNORECASE):
            return True
    return False

def classify_dok(text: str, session_context_dok: float | None=None) -> Tuple[int, float, List[str]]:
    """
    Classify text by DOK level using multi-signal approach.

    Args:
        text: The prompt text to classify
        session_context_dok: Rolling average DOK for the current session.
            When provided and no keywords match, the classifier defaults
            to round(session_context_dok) instead of a fixed DOK 2.

    Returns:
        (dok_level, confidence, matched_keywords)
        DOK 0 indicates a system message (should be excluded from counts).
    """
    if not text:
        return (2, 0.3, [])
    if is_system_message(text):
        return (0, 0.0, ['[system_message]'])
    text_lower = text.lower()
    scores = {1: 0, 2: 0, 3: 0, 4: 0}
    matched = []
    for level, patterns in DOK_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower):
                scores[level] += 1
                match = re.search(pattern, text_lower)
                if match:
                    matched.append(match.group().strip())
    total_matches = sum(scores.values())
    if total_matches == 0:
        if session_context_dok is not None:
            context_level = max(1, min(4, round(session_context_dok)))
            return (context_level, 0.2, ['[session_context]'])
        return (2, 0.3, [])
    max_score = max(scores.values())
    level = min((k for k, v in scores.items() if v == max_score))
    confidence = max_score / total_matches if total_matches > 0 else 0.3
    return (level, min(confidence, 1.0), matched[:5])

def detect_compression(text: str, session_prompt_index: int) -> bool:
    """Detect compressed intent (short prompts carrying complex meaning).

    A prompt is compressed when it's short (<=8 words), appears after
    context is established (index > 2), and either matches a known
    compression pattern or is very short (<=4 words) deep in a session.
    """
    if not text:
        return False
    word_count = len(text.split())
    if word_count <= COMPRESSION_MAX_WORDS and session_prompt_index > 2:
        text_lower = text.lower().strip()
        for pattern in COMPRESSION_INDICATORS:
            if re.match(pattern, text_lower):
                return True
        if 2 <= word_count <= 4 and session_prompt_index > 5:
            return True
    return False

def calculate_adt_zone(dok_adjusted: float, tm_tier: int, trajectory: str | None=None, threshold: int=5) -> str:
    """
    Calculate diagnostic zone from DOK x TM matrix.

    Trajectory-aware: when trajectory is 'improving' (DOK trending
    upward over the measurement window), the zone is upgraded one
    level toward Frontier. TM is stable at the baseline level for
    most practitioners, so DOK growth is the primary trajectory
    signal. The upgrade reflects that sustained cognitive growth
    indicates a healthier collaboration relationship than the
    static position alone would suggest.

    Zone hierarchy (low -> high):
      Overpowered -> Underutilizing -> Expected -> Growing -> Frontier
    Orthogonal zone (high DOK, low TM):
      Thinking Ahead
    """

    def _resolve_threshold(value):
        return 5
    if dok_adjusted >= 3.0:
        dok_band = 4
    elif dok_adjusted >= 2.5:
        dok_band = 3
    elif dok_adjusted >= 2.0:
        dok_band = 2
    else:
        dok_band = 1
    if tm_tier >= _resolve_threshold(threshold):
        if dok_band >= 3:
            zone = 'Frontier'
        elif dok_band == 2:
            zone = 'Underutilizing'
        else:
            zone = 'Overpowered'
    elif tm_tier >= 3:
        if dok_band >= 4:
            zone = 'Frontier'
        elif dok_band == 3:
            zone = 'Growing'
        elif dok_band == 2:
            zone = 'Expected'
        else:
            zone = 'Overpowered'
    elif dok_band >= 3:
        zone = 'Thinking Ahead'
    elif dok_band == 2:
        zone = 'Growing'
    else:
        zone = 'Expected'
    if trajectory == 'improving' and zone not in ('Frontier', 'Thinking Ahead'):
        upgrade_map = {'Overpowered': 'Underutilizing', 'Underutilizing': 'Growing', 'Expected': 'Growing', 'Growing': 'Frontier'}
        zone = upgrade_map.get(zone, zone)
    return zone

def estimate_tm_tier(session_data: dict) -> int:
    """Estimate Orchestra tier from session characteristics."""
    has_subagents = session_data.get('has_subagents', False)
    prompt_count = session_data.get('prompt_count', 0)
    unique_tools = session_data.get('unique_tools', 0)
    if has_subagents and prompt_count > 50:
        return 5
    elif has_subagents or (unique_tools > 5 and prompt_count > 20):
        return 4
    elif prompt_count > 10 and unique_tools > 3:
        return 3
    elif prompt_count > 3:
        return 2
    else:
        return 1

def aggregate_session_metadata(session_meta: dict, session_ids: set) -> dict:
    """Combine metadata from multiple sessions into one aggregated dict.

    Used when analyzing all sessions from a single day. Produces a
    consistent combined_meta regardless of whether sessions have
    the newer 'tools_seen' field or only the legacy 'unique_tools' count.
    """
    all_tools: set = set()
    has_tools_seen = False
    for sid in session_ids:
        meta = session_meta.get(sid, {})
        if meta.get('tools_seen'):
            has_tools_seen = True
            all_tools.update(meta['tools_seen'])
    if not has_tools_seen:
        unique_tools_count = sum((session_meta.get(sid, {}).get('unique_tools', 0) for sid in session_ids))
    else:
        unique_tools_count = len(all_tools)
    return {'prompt_count': sum((session_meta.get(sid, {}).get('prompt_count', 0) for sid in session_ids)), 'unique_tools': unique_tools_count, 'has_subagents': any((session_meta.get(sid, {}).get('has_subagents', False) for sid in session_ids)), 'accumulated_tokens': sum((session_meta.get(sid, {}).get('accumulated_tokens', 0) for sid in session_ids)), 'accumulated_input': sum((session_meta.get(sid, {}).get('accumulated_input', 0) for sid in session_ids)), 'accumulated_output': sum((session_meta.get(sid, {}).get('accumulated_output', 0) for sid in session_ids))}

def get_zone_nudges(zone: str) -> List[str]:
    """Get actionable nudges for a diagnostic zone."""
    return ZONE_NUDGES.get(zone, ZONE_NUDGES['Expected'])

def get_zone_reflection(zone: str) -> str:
    """Get reflection question for a diagnostic zone."""
    return ZONE_REFLECTIONS.get(zone, 'What could you explore more deeply?')
