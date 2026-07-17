/** Small allow-list Markdown renderer. Input is escaped before any Markdown is recognized. */

/** Strip sentinel-capable private-use characters before inline-code tokenization. */
const PRIVATE_USE = /[\uE000-\uF8FF]/g;
const FENCE = /^```([^\s`]*)\s*$/;

/** Escape HTML-significant and private-use characters; ampersand-first ordering is required. */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;")
    .replace(PRIVATE_USE, (character) => `&#${character.charCodeAt(0)};`);
}

/** Escape untrusted input, then render the supported safe block and inline Markdown subset. */
export function renderSafeMarkdown(markdown: string): string {
  const escaped = escapeHtml(markdown.replace(/\r\n?/g, "\n"));
  const lines = escaped.split("\n");
  const blocks: string[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (line.trim() === "") {
      index += 1;
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      const language = fence[1] ? ` data-language="${fence[1]}"` : "";
      blocks.push(`<div class="code-block"><pre><code${language}>${code.join("\n")}</code></pre><button class="copy-code" type="button">Copy</button></div>`);
      continue;
    }

    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      blocks.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*(?:---+|___+|\*\*\*+)\s*$/.test(line)) {
      blocks.push("<hr>");
      index += 1;
      continue;
    }

    if (/^&gt;\s?/.test(line)) {
      const quoted: string[] = [];
      while (index < lines.length && /^&gt;\s?/.test(lines[index])) {
        quoted.push(lines[index++].replace(/^&gt;\s?/, ""));
      }
      blocks.push(`<blockquote>${quoted.map(renderInline).join("<br>")}</blockquote>`);
      continue;
    }

    const unordered = /^\s*[-*+]\s+(.+)$/.exec(line);
    const ordered = /^\s*\d+[.)]\s+(.+)$/.exec(line);
    if (unordered || ordered) {
      const tag = unordered ? "ul" : "ol";
      const pattern = unordered ? /^\s*[-*+]\s+(.+)$/ : /^\s*\d+[.)]\s+(.+)$/;
      const items: string[] = [`<li>${renderInline((unordered ?? ordered)![1])}</li>`];
      index += 1;
      while (index < lines.length) {
        const item = pattern.exec(lines[index]);
        if (!item) break;
        items.push(`<li>${renderInline(item[1])}</li>`);
        index += 1;
      }
      blocks.push(`<${tag}>${items.join("")}</${tag}>`);
      continue;
    }

    const paragraph: string[] = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() !== "" && !startsBlock(lines[index])) {
      paragraph.push(lines[index++]);
    }
    blocks.push(`<p>${paragraph.map(renderInline).join("<br>")}</p>`);
  }

  return blocks.join("");
}

/** Mirror the dispatch grammar when deciding whether a paragraph must terminate. */
function startsBlock(line: string): boolean {
  return FENCE.test(line) || /^#{1,3}\s+|^&gt;\s?|^\s*(?:[-*+]\s+|\d+[.)]\s+|---+\s*$|___+\s*$|\*\*\*+\s*$)/.test(line);
}

/** Parse already-escaped inline text using private-use sentinels for code spans. */
function renderInline(value: string): string {
  const code: string[] = [];
  let rendered = value.replace(/\[([^\]\n]+)\]\(([^)\s`]+)\)/g, (whole, label: string, encodedHref: string) => {
    const href = decodeEscapedAttribute(encodedHref);
    if (!isHttpUrl(href)) return whole;
    return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${renderInline(label)}</a>`;
  });
  rendered = rendered.replace(/`([^`\n]+)`/g, (_, body: string) => {
    const token = `\uE000${code.length}\uE001`;
    code.push(`<code>${body}</code>`);
    return token;
  });
  rendered = rendered
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
    .replace(/_([^_\n]+)_/g, "<em>$1</em>");
  return rendered.replace(/\uE000(\d+)\uE001/g, (_, position: string) => code[Number(position)] ?? "");
}

/** Reverse HTML escaping so URL validation sees the original captured href. */
function decodeEscapedAttribute(value: string): string {
  return value
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");
}

/** Allow only absolute HTTP(S) links. */
function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}
