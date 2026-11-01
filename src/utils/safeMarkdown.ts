import DOMPurify from "isomorphic-dompurify";
import { Marked } from "marked";

// proposals are community-written, so only plain document markup gets through.
// anything that could style the page or pose as the contribute box stays out
const ALLOWED_TAGS = [
  "a",
  "blockquote",
  "br",
  "code",
  "del",
  "details",
  "em",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "hr",
  "img",
  "li",
  "ol",
  "p",
  "pre",
  "s",
  "strong",
  "sub",
  "summary",
  "sup",
  "table",
  "tbody",
  "td",
  "th",
  "thead",
  "tr",
  "ul",
];
const ALLOWED_ATTR = ["href", "src", "alt", "title"];

const parser = new Marked({
  renderer: {
    checkbox: ({ checked }) => (checked ? "[x] " : "[ ] "),
  },
});

export const safeMarkdown = (markdown: string): string =>
  DOMPurify.sanitize(parser.parse(markdown, { async: false }), {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
  });
