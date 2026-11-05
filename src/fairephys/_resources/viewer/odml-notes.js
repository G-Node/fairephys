/**
 * odML Notes - Browser-side annotation for odML documents
 * Stores notes in localStorage, keyed by section/property path
 */
(function() {
  'use strict';

  const STORAGE_KEY = 'odml-notes';

  const load = () => {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; }
    catch { return {}; }
  };

  const save = (notes) => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(notes));
    updateCount();
  };

  const updateCount = () => {
    const el = document.getElementById('note-count');
    if (el) {
      const n = Object.keys(load()).length;
      el.textContent = `${n} note${n !== 1 ? 's' : ''}`;
    }
  };

  const getPath = (cell) => {
    const row = cell.closest('tr');
    const propName = row?.querySelector('.prop-name')?.textContent.trim() || 'unknown';
    const parts = [];

    for (let el = cell.parentElement; el; el = el.parentElement) {
      const header =
        el.classList?.contains('section') ? el.querySelector('.section-header h2') :
        el.classList?.contains('subsection') ? el.querySelector('.subsection-header h3') :
        el.matches?.('details.nested-item, details.nested-item-deep') ? el.querySelector('summary') :
        null;
      if (header) parts.unshift(header.textContent.trim());
    }

    parts.push(propName);
    return parts.join('/');
  };

  const download = (filename, content) => {
    const a = Object.assign(document.createElement('a'), {
      href: URL.createObjectURL(new Blob([content], { type: 'application/json' })),
      download: filename
    });
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const init = () => {
    const notes = load();

    // Initialize note cells
    document.querySelectorAll('.prop-notes').forEach(cell => {
      const path = getPath(cell);
      if (notes[path]) cell.textContent = notes[path];

      cell.onblur = () => {
        const current = load();
        const value = cell.textContent.trim();
        value ? (current[path] = value) : delete current[path];
        save(current);
      };
    });

    // Export
    document.getElementById('export-notes')?.addEventListener('click', () => {
      const title = document.title.replace(/[^a-zA-Z0-9-_]/g, '-') || 'odml';
      download(`${title}-notes.json`, JSON.stringify({
        exported: new Date().toISOString(),
        document: document.title,
        notes: load()
      }, null, 2));
    });

    // Import
    const importFile = document.getElementById('import-file');
    document.getElementById('import-notes')?.addEventListener('click', () => importFile?.click());
    importFile?.addEventListener('change', e => {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = evt => {
        try {
          const data = JSON.parse(evt.target.result);
          save(data.notes || data);
          location.reload();
        } catch (err) {
          alert('Invalid JSON: ' + err.message);
        }
      };
      reader.readAsText(file);
    });

    // Clear
    document.getElementById('clear-notes')?.addEventListener('click', () => {
      if (confirm('Clear all notes?')) {
        localStorage.removeItem(STORAGE_KEY);
        location.reload();
      }
    });

    updateCount();
  };

  // Initialize on DOM ready or immediately if already loaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
