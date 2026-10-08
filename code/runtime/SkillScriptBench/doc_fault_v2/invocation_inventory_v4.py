"""Public Markdown examples and Python interface facts; no evaluator imports."""
import ast
import re
from pathlib import Path

from markdown_it import MarkdownIt


def inventory(text):
    rows = []
    for token in MarkdownIt('commonmark').parse(text):
        if token.type in ('fence', 'code_block'):
            language = token.info.split()[0].lower() if token.info.strip() else ''
            if language in ('json', 'yaml', 'yml', 'csv', 'xml', 'html', 'diff', 'mermaid'):
                continue
            kind = 'python_example' if language == 'python' else 'command_block'
            if language not in ('python', 'bash', 'sh', 'shell', 'zsh', 'console', 'powershell', 'ps1'):
                kind = 'unclassified_block'
            rows.append(dict(kind=kind, line=token.map[0] + 1, end_line=token.map[1], text=token.content.rstrip('\n')))
        elif token.type == 'inline':
            for child in token.children or []:
                if child.type != 'code_inline':
                    continue
                value = child.content
                if re.match(r'^(?:python[\d.]*|bash|sh|node|npx|npm|uv|pip[\d.]*|pytest|pwsh|docker|make)\s', value):
                    kind = 'inline_command'
                elif value.startswith('--'):
                    kind = 'inline_argument'
                elif re.search(r'(?:^|/)[\w.-]+\.(?:py|sh|js|mjs|ts|ps1)(?:\s|$)', value):
                    kind = 'inline_command' if re.search(r'\s', value) else 'path_reference'
                else:
                    continue
                # Keep the containing Markdown block so a correction may also
                # repair the prose explaining an inline option or command.
                rows.append(dict(kind=kind, line=token.map[0] + 1, end_line=token.map[1], text=value))
    for index, row in enumerate(rows):
        row['id'] = f"invocation-{index + 1}"
    return rows


def value(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return dict(expression=ast.unparse(node))


def python_interfaces(scripts):
    result = []
    for path, source in sorted(scripts.items()):
        if not path.endswith('.py'):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            result.append(dict(path=path, status='UNPARSEABLE'))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_argument':
                result.append(dict(path=path, line=node.lineno, kind='argparse_declaration',
                    flags=[value(n) for n in node.args],
                    keywords={k.arg: value(k.value) for k in node.keywords if k.arg},
                    quote=ast.get_source_segment(source, node),
                    claim='declaration_only_not_whole_program_behavior'))
    return result


def neutral_request(text):
    tokens = MarkdownIt('commonmark').parse(text)
    spans = []
    for i, token in enumerate(tokens):
        if token.type != 'heading_open' or tokens[i + 1].content.strip().casefold() != 'observed problem':
            continue
        level = int(token.tag[1:])
        end = len(text.splitlines())
        for following in tokens[i + 1:]:
            if following.type == 'heading_open' and int(following.tag[1:]) <= level:
                end = following.map[0]
                break
        spans.append((token.map[0], end))
    lines = text.splitlines(keepends=True)
    for start, end in reversed(spans):
        del lines[start:end]
    return ''.join(lines), dict(removed_heading='Observed problem', removed_sections=len(spans),
        hidden_or_mutation_labels_used=False)
