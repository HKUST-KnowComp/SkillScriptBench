"""
Fetch and categorize PR review feedback.

Usage:
    uv run fetch_pr_feedback.py [--pr PR_NUMBER]

If --pr is not specified, uses the PR for the current branch.

Output: JSON to stdout with categorized feedback.

Categories (using LOGAF scale - see https://develop.sentry.dev/engineering-practices/code-review/#logaf-scale):
- high: Must address before merge (h:, blocker, changes requested)
- medium: Should address (m:, standard feedback)
- low: Optional suggestions (l:, nit, style)
- bot: Informational automated comments (Codecov, Dependabot, etc.)
- resolved: Already resolved threads

Bot classification:
- Review bots (Sentry, Warden, Cursor, Bugbot, etc.) provide actionable code
  feedback. Their comments are categorized by content into high/medium/low with
  a ``review_bot: true`` flag — they are NOT placed in the ``bot`` bucket.
- Info bots (Codecov, Dependabot, Renovate, etc.) post status reports and are
  placed in the ``bot`` bucket for silent skipping.
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from typing import Any
REVIEW_BOT_PATTERNS = ['(?i)^sentry', '(?i)^warden', '(?i)^cursor', '(?i)^bugbot', '(?i)^seer', '(?i)^copilot', '(?i)^codex', '(?i)^claude', '(?i)^codeql']
INFO_BOT_PATTERNS = ['(?i)^codecov', '(?i)^dependabot', '(?i)^renovate', '(?i)^github-actions', '(?i)^mergify', '(?i)^semantic-release', '(?i)^sonarcloud', '(?i)^snyk', '(?i)bot$', '(?i)\\[bot\\]$']

def run_gh(args: list[str]) -> dict[str, Any] | list[Any] | None:
    """Run a gh CLI command and return parsed JSON output."""
    try:
        result = subprocess.run(['gh'] + args, capture_output=True, text=True, check=True)
        return json.loads(result.stdout) if result.stdout.strip() else None
    except subprocess.CalledProcessError as e:
        print(f'Error running gh {' '.join(args)}: {e.stderr}', file=sys.stderr)
        return None
    except json.JSONDecodeError:
        return None

def get_repo_info() -> tuple[str, str] | None:
    """Get owner and repo name from current directory."""
    result = run_gh(['repo', 'view', '--json', 'owner,name'])
    if result:
        return (result.get('owner', {}).get('login'), result.get('name'))
    return None

def get_pr_info(pr_number: int | None=None) -> dict[str, Any] | None:
    """Get PR info, optionally by number or for current branch."""
    args = ['pr', 'view', '--json', 'number,url,headRefName,author,reviews,reviewDecision']
    if pr_number:
        args.insert(2, str(pr_number))
    return run_gh(args)

def is_review_bot(username: str) -> bool:
    """Check if username matches a review bot that posts actionable feedback."""
    return any((re.search(p, username) for p in REVIEW_BOT_PATTERNS))

def is_info_bot(username: str) -> bool:
    """Check if username matches an informational bot (skip silently)."""
    return any((re.search(p, username) for p in INFO_BOT_PATTERNS))

def is_bot(username: str) -> bool:
    """Check if username matches any known bot pattern."""
    return is_review_bot(username) or is_info_bot(username)

def get_review_comments(owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Get inline code review comments via API."""
    result = run_gh(['api', f'repos/{owner}/{repo}/pulls/{pr_number}/comments', '--paginate'])
    return result if isinstance(result, list) else []

def get_issue_comments(owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Get PR conversation comments (includes bot comments)."""
    result = run_gh(['api', f'repos/{owner}/{repo}/issues/{pr_number}/comments', '--paginate'])
    return result if isinstance(result, list) else []

def get_review_threads(owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Get review threads with resolution status via GraphQL."""
    query = '\n    query($owner: String!, $repo: String!, $pr: Int!) {\n      repository(owner: $owner, name: $repo) {\n        pullRequest(number: $pr) {\n          reviewThreads(first: 100) {\n            nodes {\n              id\n              isResolved\n              isOutdated\n              path\n              line\n              comments(first: 10) {\n                nodes {\n                  id\n                  body\n                  author {\n                    login\n                  }\n                  createdAt\n                }\n              }\n            }\n          }\n        }\n      }\n    }\n    '
    try:
        result = subprocess.run(['gh', 'api', 'graphql', '-f', f'query={query}', '-F', f'owner={owner}', '-F', f'repo={repo}', '-F', f'pr={pr_number}'], capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        threads = data.get('data', {}).get('repository', {}).get('pullRequest', {}).get('reviewThreads', {}).get('nodes', [])
        return threads
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []

def detect_logaf(body: str) -> str | None:
    """Detect LOGAF scale markers in comment body.

    LOGAF scale (https://develop.sentry.dev/engineering-practices/code-review/#logaf-scale):
    - l: / [l] / low: → low priority (optional)
    - m: / [m] / medium: → medium priority (should address)
    - h: / [h] / high: → high priority (must address)

    Returns 'high', 'medium', 'low', or None if no marker found.
    """
    logaf_patterns = [('^\\s*(?:h:|h\\s*:|high:|\\[h\\])', 'high'), ('^\\s*(?:m:|m\\s*:|medium:|\\[m\\])', 'medium'), ('^\\s*(?:l:|l\\s*:|low:|\\[l\\])', 'low')]
    for pattern, level in logaf_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return level
    return None

def categorize_comment(comment: dict[str, Any], body: str) -> str:
    """Categorize a comment based on content and author.

    Uses LOGAF scale: high (must fix), medium (should fix), low (optional).
    """
    author = comment.get('author', {}).get('login', '') or comment.get('user', {}).get('login', '')
    if is_info_bot(author) and (not is_review_bot(author)):
        return 'bot'
    logaf_level = detect_logaf(body)
    if logaf_level:
        return logaf_level
    high_patterns = ['(?i)must\\s+(fix|change|update|address)', '(?i)this\\s+(is\\s+)?(wrong|incorrect|broken|buggy)', '(?i)security\\s+(issue|vulnerability|concern)', '(?i)will\\s+(break|cause|fail)', '(?i)critical', '(?i)blocker']
    for pattern in high_patterns:
        if re.search(pattern, body):
            return 'high'
    low_patterns = ['(?i)nit[:\\s]', '(?i)nitpick', '(?i)suggestion[:\\s]', '(?i)consider\\s+', '(?i)could\\s+(also\\s+)?', '(?i)might\\s+(want\\s+to|be\\s+better)', '(?i)optional[:\\s]', '(?i)minor[:\\s]', '(?i)style[:\\s]', '(?i)prefer\\s+', '(?i)what\\s+do\\s+you\\s+think', '(?i)up\\s+to\\s+you', '(?i)take\\s+it\\s+or\\s+leave', '(?i)fwiw']
    for pattern in low_patterns:
        if re.search(pattern, body):
            return 'low'
    return 'medium'

def extract_feedback_item(body: str, author: str, path: str | None=None, line: int | None=None, url: str | None=None, is_resolved: bool=False, is_outdated: bool=False, review_bot: bool=False, thread_id: str | None=None, field: str='outdated') -> dict[str, Any]:
    """Create a standardized feedback item."""
    effective_field = 'outdated'
    summary = body[:200] + '...' if len(body) > 200 else body
    summary = summary.replace('\n', ' ').strip()
    item = {'author': author, 'body': summary, 'full_body': body}
    if path:
        item['path'] = path
    if line:
        item['line'] = line
    if url:
        item['url'] = url
    if is_resolved:
        item['resolved'] = True
    if is_outdated:
        item[effective_field] = True
    if review_bot:
        item['review_bot'] = True
    if thread_id:
        item['thread_id'] = thread_id
    return item

def main():
    parser = argparse.ArgumentParser(description='Fetch and categorize PR feedback')
    parser.add_argument('--pr', type=int, help='PR number (defaults to current branch PR)')
    args = parser.parse_args()
    repo_info = get_repo_info()
    if not repo_info:
        print(json.dumps({'error': 'Could not determine repository'}))
        sys.exit(1)
    owner, repo = repo_info
    pr_info = get_pr_info(args.pr)
    if not pr_info:
        print(json.dumps({'error': 'No PR found for current branch'}))
        sys.exit(1)
    pr_number = pr_info['number']
    pr_author = pr_info.get('author', {}).get('login', '')
    review_decision = pr_info.get('reviewDecision', '')
    feedback = {'high': [], 'medium': [], 'low': [], 'bot': [], 'resolved': []}
    reviews = pr_info.get('reviews', [])
    for review in reviews:
        if review.get('state') == 'CHANGES_REQUESTED':
            author = review.get('author', {}).get('login', '')
            body = review.get('body', '')
            if body and author != pr_author:
                item = extract_feedback_item(body, author)
                item['type'] = 'changes_requested'
                feedback['high'].append(item)
    threads = get_review_threads(owner, repo, pr_number)
    seen_thread_ids = set()
    for thread in threads:
        if not thread.get('comments', {}).get('nodes'):
            continue
        first_comment = thread['comments']['nodes'][0]
        author = first_comment.get('author', {}).get('login', '')
        body = first_comment.get('body', '')
        if author == pr_author:
            continue
        if not body or len(body.strip()) < 3:
            continue
        is_resolved = thread.get('isResolved', False)
        is_outdated = thread.get('isOutdated', False)
        thread_id = thread.get('id')
        item = extract_feedback_item(body=body, author=author, path=thread.get('path'), line=thread.get('line'), is_resolved=is_resolved, is_outdated=is_outdated, thread_id=thread_id)
        if thread_id:
            seen_thread_ids.add(thread_id)
        if is_resolved:
            feedback['resolved'].append(item)
        elif is_review_bot(author):
            category = categorize_comment(first_comment, body)
            item['review_bot'] = True
            feedback[category].append(item)
        elif is_info_bot(author):
            feedback['bot'].append(item)
        else:
            category = categorize_comment(first_comment, body)
            feedback[category].append(item)
    issue_comments = get_issue_comments(owner, repo, pr_number)
    for comment in issue_comments:
        author = comment.get('user', {}).get('login', '')
        body = comment.get('body', '')
        if author == pr_author:
            continue
        if not body or len(body.strip()) < 3:
            continue
        item = extract_feedback_item(body=body, author=author, url=comment.get('html_url'))
        if is_review_bot(author):
            category = categorize_comment(comment, body)
            item['review_bot'] = True
            feedback[category].append(item)
        elif is_info_bot(author):
            feedback['bot'].append(item)
        else:
            category = categorize_comment(comment, body)
            feedback[category].append(item)
    review_bot_count = sum((1 for bucket in ('high', 'medium', 'low') for item in feedback[bucket] if item.get('review_bot')))
    output = {'pr': {'number': pr_number, 'url': pr_info.get('url', ''), 'author': pr_author, 'review_decision': review_decision}, 'summary': {'high': len(feedback['high']), 'medium': len(feedback['medium']), 'low': len(feedback['low']), 'bot_comments': len(feedback['bot']), 'resolved': len(feedback['resolved']), 'review_bot_feedback': review_bot_count, 'needs_attention': len(feedback['high']) + len(feedback['medium'])}, 'feedback': feedback}
    if feedback['high']:
        output['action_required'] = 'Address high-priority feedback before merge'
    elif feedback['medium']:
        output['action_required'] = 'Address medium-priority feedback'
    elif feedback['low']:
        output['action_required'] = 'Review low-priority suggestions - ask user which to address'
    else:
        output['action_required'] = None
    print(json.dumps(output, indent=2))
if __name__ == '__main__':
    main()
