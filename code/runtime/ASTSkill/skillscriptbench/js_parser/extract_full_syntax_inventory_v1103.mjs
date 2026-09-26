import { parse } from "@babel/parser";
import { readFileSync } from "node:fs";

const { source, filename } = JSON.parse(readFileSync(0, "utf8"));
const plugins = [];
if ([".ts", ".tsx", ".mts", ".cts"].some(s => filename.endsWith(s))) plugins.push("typescript");
if ([".jsx", ".tsx"].some(s => filename.endsWith(s))) plugins.push("jsx");
const tree = parse(source, { sourceType: "unambiguous", plugins });
const rows = [];
const signatures = [];
const imports = {};
const shadowed = new Map();
const functions = new Set(["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression", "ObjectMethod", "ClassMethod", "ClassPrivateMethod"]);
const byte = offset => Buffer.byteLength(source.slice(0, offset), "utf8");
function names(pattern) {
  if (!pattern) return [];
  if (pattern.type === "Identifier") return [pattern.name];
  if (pattern.type === "AssignmentPattern") return names(pattern.left);
  if (pattern.type === "RestElement") return names(pattern.argument);
  if (pattern.type === "ArrayPattern") return pattern.elements.flatMap(names);
  if (pattern.type === "ObjectPattern") return pattern.properties.flatMap(p => names(p.value || p.argument));
  return [];
}
function localBindings(node, out = new Set()) {
  if (!node || typeof node.type !== "string" || functions.has(node.type)) return out;
  if (node.type === "VariableDeclarator" && !functions.has(node.init?.type)) names(node.id).forEach(n => out.add(n));
  if (node.type === "AssignmentExpression") names(node.left).forEach(n => out.add(n));
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) value.forEach(v => localBindings(v, out));
    else if (value && typeof value.type === "string") localBindings(value, out);
  }
  return out;
}
shadowed.set("<module>", localBindings(tree.program));
const clean = value => {
  if (Array.isArray(value)) return value.map(clean);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).filter(([k]) => !["start", "end", "loc", "extra", "leadingComments", "trailingComments", "innerComments"].includes(k)).map(([k, v]) => [k, clean(v)]));
};
function walk(node, parentId = null, role = "root", scope = "<module>", parent = null) {
  if (!node || typeof node.type !== "string" || node.type.endsWith("Comment")) return;
  const name = node.id?.name || node.key?.name || node.key?.value || (parent?.type === "VariableDeclarator" ? parent.id?.name : null);
  const isFunction = functions.has(node.type);
  const isClass = ["ClassDeclaration", "ClassExpression"].includes(node.type);
  const nextScope = isFunction || isClass ? `${scope === "<module>" ? "" : scope + "."}${name || "<anonymous>"}` : scope;
  if (isFunction) shadowed.set(nextScope, new Set([...node.params.flatMap(names), ...localBindings(node.body)]));
  const id = rows.length;
  const callable = ["CallExpression", "OptionalCallExpression", "NewExpression"].includes(node.type);
  const facts = {};
  if (callable) {
    facts.callee = source.slice(node.callee.start, node.callee.end);
    let root = node.callee;
    while (["MemberExpression", "OptionalMemberExpression"].includes(root.type)) root = root.object;
    facts.shadowed_root = root.type === "Identifier" && (!!shadowed.get(nextScope)?.has(root.name) || shadowed.get("<module>").has(root.name));
  }
  if (node.type === "ImportDeclaration") {
    for (const s of node.specifiers) imports[s.local.name] = { module: node.source.value, name: s.imported?.name || "default" };
  }
  rows.push({ id, parent: parentId, role, type: node.type, start: byte(node.start), end: byte(node.end),
    line: node.loc.start.line, end_line: node.loc.end.line, symbol: nextScope,
    editable: !["File", "Program", "Identifier", "PrivateName"].includes(node.type) && !isFunction && !isClass,
    defines: isFunction ? nextScope : null, facts });
  if (isFunction && name) signatures.push({ symbol: nextScope, params: clean(node.params), async: !!node.async, generator: !!node.generator });
  for (const [key, value] of Object.entries(node)) {
    if (["loc", "extra", "comments", "tokens", "errors"].includes(key) || key.endsWith("Comments")) continue;
    if (Array.isArray(value)) value.forEach((child, i) => walk(child, id, `${key}[${i}]`, nextScope, node));
    else if (value && typeof value.type === "string") walk(value, id, key, nextScope, node);
  }
}
walk(tree.program);
// Identifiers are legitimate exact expression nodes, but declaration names are not.
for (const row of rows) if (row.type === "Identifier") row.editable = !["id", "key"].includes(row.role) && !row.role.startsWith("params[");
process.stdout.write(JSON.stringify({ nodes: rows, signatures, imports }));
