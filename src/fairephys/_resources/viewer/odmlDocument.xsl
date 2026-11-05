<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">

<xsl:template match="odML">
  <html>
    <head>
      <meta charset="UTF-8"/>
      <title>
        <xsl:value-of select="section[name='Dataset']/property[name='short name']/value"/>
        <xsl:if test="not(section[name='Dataset']/property[name='short name']/value)">odML Document</xsl:if>
      </title>
      <link rel="stylesheet" href="odml-style.css"/>
      <script src="odml-notes.js"/>
    </head>
    <body>
      <div class="notes-toolbar">
        <span id="note-count">0 notes</span>
        <button id="export-notes">Export</button>
        <button id="import-notes">Import</button>
        <button id="clear-notes">Clear</button>
        <input type="file" id="import-file" accept=".json" style="display:none"/>
      </div>
      <div class="container">
        <xsl:call-template name="sidebar"/>
        <main class="main">
          <xsl:for-each select="section"><xsl:call-template name="section"/></xsl:for-each>
          <div class="footer">
            Generated from odML &#x2022;
            <a href="https://openminds.docs.om-i.org/">openMINDS</a>
          </div>
        </main>
      </div>
    </body>
  </html>
</xsl:template>

<xsl:template name="sidebar">
  <nav class="sidebar">
    <!-- the collection (umbrella project) the dataset belongs to, else the dataset -->
    <h1>
      <xsl:choose>
        <xsl:when test="section[name='Dataset']/section[name='Project']/property[name='name']/value">
          <xsl:value-of select="section[name='Dataset']/section[name='Project']/property[name='name']/value"/>
        </xsl:when>
        <xsl:when test="section[name='Dataset']/property[name='short name']/value">
          <xsl:value-of select="section[name='Dataset']/property[name='short name']/value"/>
        </xsl:when>
        <xsl:otherwise>odML</xsl:otherwise>
      </xsl:choose>
    </h1>
    <div class="meta">
      <div>Author: <xsl:value-of select="author"/></div>
      <div>Date: <xsl:value-of select="date"/></div>
      <div>Version: <xsl:value-of select="version"/></div>
    </div>
    <ul>
      <xsl:for-each select="section">
        <li>
          <a class="nav-section" href="#{generate-id()}"><xsl:value-of select="name"/></a>
          <xsl:if test="section">
            <ul><xsl:for-each select="section">
              <li><a class="nav-child" href="#{generate-id()}"><xsl:value-of select="name"/></a></li>
            </xsl:for-each></ul>
          </xsl:if>
        </li>
      </xsl:for-each>
    </ul>
  </nav>
</xsl:template>

<xsl:template name="type-badge">
  <xsl:param name="class"/>
  <xsl:if test="type and type != 'n.s.'">
    <span class="{$class}"><xsl:call-template name="type-link"/></span>
  </xsl:if>
</xsl:template>

<xsl:template name="type-link">
  <xsl:variable name="typename" select="substring-after(type, '.')"/>
  <xsl:choose>
    <xsl:when test="$typename != ''">
      <a href="https://openminds.docs.om-i.org/en/latest/search.html?q={$typename}" target="_blank" title="Search schema"><xsl:value-of select="type"/></a>
    </xsl:when>
    <xsl:otherwise><xsl:value-of select="type"/></xsl:otherwise>
  </xsl:choose>
</xsl:template>

<xsl:template name="section">
  <div class="section" id="{generate-id()}">
    <div class="section-header">
      <h2><xsl:value-of select="name"/></h2>
      <xsl:call-template name="type-badge"><xsl:with-param name="class">section-type</xsl:with-param></xsl:call-template>
    </div>
    <div class="section-body">
      <xsl:if test="definition"><p class="definition"><xsl:value-of select="definition"/></p></xsl:if>
      <xsl:if test="property"><xsl:call-template name="property-table"/></xsl:if>
      <xsl:for-each select="section"><xsl:call-template name="subsection"/></xsl:for-each>
    </div>
  </div>
</xsl:template>

<xsl:template name="subsection">
  <div class="subsection" id="{generate-id()}">
    <div class="subsection-header">
      <h3><xsl:value-of select="name"/></h3>
      <xsl:call-template name="type-badge"><xsl:with-param name="class">type-badge</xsl:with-param></xsl:call-template>
    </div>
    <div class="subsection-body">
      <xsl:if test="definition"><p class="definition"><xsl:value-of select="definition"/></p></xsl:if>
      <xsl:if test="property"><xsl:call-template name="property-table"/></xsl:if>
      <xsl:for-each select="section"><xsl:call-template name="nested-section"/></xsl:for-each>
    </div>
  </div>
</xsl:template>

<xsl:template name="nested-section">
  <details class="nested-item">
    <summary>
      <xsl:value-of select="name"/>
      <xsl:call-template name="type-badge"><xsl:with-param name="class">type-badge sm</xsl:with-param></xsl:call-template>
    </summary>
    <div class="nested-content">
      <xsl:if test="property"><xsl:call-template name="property-table"/></xsl:if>
      <xsl:for-each select="section"><xsl:call-template name="nested-section"/></xsl:for-each>
    </div>
  </details>
</xsl:template>

<xsl:template name="property-table">
  <table>
    <thead><tr>
      <th>Property</th><th>Value</th><th>Type</th><th>Unit</th>
      <th>Definition</th><th>Source</th><th>Notes</th>
    </tr></thead>
    <tbody><xsl:for-each select="property">
      <tr>
        <td class="prop-name">
          <xsl:if test="reference and reference != ''">
            <xsl:attribute name="data-ref"><xsl:value-of select="reference"/></xsl:attribute>
          </xsl:if>
          <xsl:value-of select="name"/>
        </td>
        <td>
          <xsl:attribute name="class">prop-value<xsl:if test="not(value) or value = ''"> empty</xsl:if></xsl:attribute>
          <xsl:choose>
            <xsl:when test="value and value != ''"><xsl:value-of select="value"/></xsl:when>
            <xsl:otherwise>(empty)</xsl:otherwise>
          </xsl:choose>
        </td>
        <td class="prop-type"><xsl:value-of select="type"/></td>
        <td class="prop-unit"><xsl:value-of select="unit"/></td>
        <td class="prop-def"><xsl:value-of select="definition"/></td>
        <td class="prop-origin"><xsl:value-of select="value_origin"/></td>
        <td class="prop-notes" contenteditable="true" data-placeholder="Add note..."/>
      </tr>
    </xsl:for-each></tbody>
  </table>
</xsl:template>

</xsl:stylesheet>
