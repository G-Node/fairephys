# odML Viewer

Stylesheet and scripts that render an odML XML document as an HTML page.

## Files

| File | Purpose |
|------|---------|
| `odmlDocument.xsl` | XSLT transformation (XML to HTML) |
| `odml-style.css` | Visual styling |
| `odml-notes.js` | Browser-side annotation (localStorage) |

## Usage

`fairephys.metadata.save_odml()` writes the XML without a stylesheet reference (pure data) and calls `generate_html_viewer()`, which applies the XSL with the CSS and JS inlined. The resulting `<dataset>.odml.html` next to the XML is self-contained: it opens offline from any location and needs none of these files. When `<dataset>.instances.yaml` exists, links to the resolved openMINDS instances are added.

## Features

- Sidebar navigation with section links
- Collapsible nested sections
- Property tables with all odML fields
- Clickable reference links (openMINDS, Ontobee, etc.)
- Editable notes column (persisted in browser localStorage)
- Export/import notes as JSON
- Responsive layout
