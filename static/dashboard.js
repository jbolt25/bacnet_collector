(() => {
  const form = document.getElementById('scan-form');
  const action = document.getElementById('scan-action');
  const state = document.getElementById('scan-state');
  const results = document.getElementById('scan-results');
  const message = document.getElementById('action-message');
  let timer;
  let generation = 0;
  let busy = false;
  let rendered = '';
  let previousScan;
  action.disabled = true;

  function fetchTimed(url, options = {}) {
    return fetch(url, {...options, signal: AbortSignal.timeout(10000)});
  }

  async function refreshSaved() {
    const response = await fetchTimed('/', {cache: 'no-store'});
    if (!response.ok) throw new Error('saved results could not refresh');
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    for (const id of ['saved-scans', 'scan-history', 'audit-log', 'approved-devices']) {
      const current = document.getElementById(id);
      const updated = page.getElementById(id);
      // Never overwrite a name the operator is currently editing.
      if (current && updated && !current.contains(document.activeElement)) {
        current.replaceChildren(...updated.childNodes);
      }
    }
  }

  function setAction(running) {
    form.dataset.running = running ? 'true' : 'false';
    action.textContent = running ? 'Stop scan' : 'Run operator scan now';
    action.classList.toggle('stop-action', running);
  }

  function text(parent, value, tag = 'div') {
    const node = document.createElement(tag);
    node.textContent = value == null || value === '' ? '—' : String(value);
    parent.appendChild(node);
    return node;
  }

  function render(scan) {
    const signature = JSON.stringify(scan);
    if (signature === rendered) return;
    rendered = signature;
    results.replaceChildren();
    if (scan && scan.error) text(results, `Reason: ${scan.error}. Saved points are retained.`, 'p').className = 'bad';
    if (!scan || !scan.devices || scan.devices.length === 0) {
      text(results, !scan ? 'No scan running.' : scan.status === 'running'
        ? 'Waiting for devices…' : 'No devices returned by this scan.', 'p');
      return;
    }
    scan.devices.forEach((device) => {
      const article = document.createElement('article');
      text(article, `${device.object_name || 'Unnamed device'} · device ${device.instance}`, 'h3');
      text(article, `${device.address} · ${device.approved ? 'approved' : 'discovered'}`);
      if (!device.points || device.points.length === 0) {
        if (device.object_error) {
          text(article, `No points returned: ${device.object_error}`, 'p');
        } else if (scan.status === 'running') {
          text(article, 'Reading object list and points…', 'p');
        } else {
          text(article, 'No points returned by this device.', 'p');
        }
      } else {
        const table = document.createElement('table');
        const head = document.createElement('tr');
        ['Object', 'Name', 'Value', 'Status'].forEach((label) => text(head, label, 'th'));
        table.appendChild(head);
        device.points.forEach((point) => {
          const row = document.createElement('tr');
          text(row, point.object_id, 'td');
          text(row, point.object_name, 'td');
          text(row, [point.value_text, point.units].filter((value) => value != null).join(' '), 'td');
          text(row, point.read_error || (point.approved ? 'approved' : 'discovered'), 'td');
          table.appendChild(row);
        });
        const container = document.createElement('div');
        container.className = 'table-scroll';
        container.tabIndex = 0;
        container.setAttribute('role', 'region');
        container.setAttribute('aria-label', `Live points for device ${device.instance}`);
        container.appendChild(table);
        article.appendChild(container);
      }
      results.appendChild(article);
    });
  }

  async function poll() {
    clearTimeout(timer);
    const ticket = generation;
    let delay = 5000;
    try {
      const response = await fetchTimed('/api/scan/status', {cache: 'no-store'});
      if (!response.ok) throw new Error('status request failed');
      const data = await response.json();
      if (ticket !== generation) return;
      const scans = data.saved_scans || [];
      const scan = scans.find((item) => item.status === 'running') || scans[0];
      action.disabled = busy;
      if (!scan) {
        state.textContent = 'Idle — no scans saved';
        setAction(false);
        render(null);
        return;
      }
      setAction(scan.status === 'running');
      const label = scan.error === 'scan stopped by operator' ? 'stopped' : scan.status;
      state.textContent = scan.status === 'running'
        ? `Scanning… ${scan.device_count} devices, ${scan.point_count} points`
        : `Scan ${label}: ${scan.device_count} devices, ${scan.point_count} points`;
      render(scan);
      if (scan.status === 'running') delay = 1000;
      const refresh = previousScan && scan.status !== 'running'
        && (previousScan.status === 'running' || previousScan.id !== scan.id);
      previousScan = {id: scan.id, status: scan.status};
      if (refresh) {
        try { await refreshSaved(); }
        catch (error) { message.textContent = 'Scan saved. Refresh the page to update saved results.'; }
      }
    } catch (error) {
      if (ticket !== generation) return;
      state.textContent = 'Unable to read scan progress; retrying…';
      action.disabled = true;
      delay = 2000;
    } finally {
      if (ticket === generation && !busy) timer = setTimeout(poll, delay);
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    generation += 1; // Ignore a status response already in flight.
    action.disabled = true;
    if (timer) clearTimeout(timer);
    const stopping = form.dataset.running === 'true';
    state.textContent = stopping ? 'Stopping scan…' : 'Starting scan…';
    message.textContent = '';
    const body = new URLSearchParams(new FormData(form));
    try {
      const response = await fetchTimed(stopping ? '/api/rescan/cancel' : '/api/rescan', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
        body
      });
      if (!response.ok) {
        if (response.status === 403) throw new Error('Action token expired. Refresh this page and try again.');
        if (response.status === 409) throw new Error('Scan state changed. Checking current status…');
        throw new Error(`Request rejected (${response.status}).`);
      }
      if (!stopping) setAction(true);
    } catch (error) {
      message.textContent = `${stopping ? 'Stop' : 'Start'} request not confirmed: ${error.message}`;
    } finally {
      busy = false;
      await poll();
    }
  });

  document.addEventListener('submit', async (event) => {
    const localForm = event.target;
    const path = new URL(localForm.action).pathname;
    if (!['/api/scan/rename', '/api/scan/delete', '/api/scan-point/delete', '/api/point/delete'].includes(path)) return;
    event.preventDefault();
    if (localForm.dataset.pending === 'true') return;
    if (path.endsWith('/delete') && !window.confirm(
      path === '/api/scan/delete'
        ? 'Delete this saved scan and its collected points from this Pi? This cannot be undone. BACnet devices are not affected.'
        : path === '/api/point/delete'
          ? 'Disable local trending for this point? BACnet devices are not affected.'
          : 'Remove this point from the saved scan? This cannot be undone. BACnet devices are not affected.'
    )) return;
    localForm.dataset.pending = 'true';
    const button = localForm.querySelector('button');
    button.disabled = true;
    message.textContent = '';
    try {
      const response = await fetchTimed(path, {method: 'POST', body: new URLSearchParams(new FormData(localForm))});
      if (!response.ok) throw new Error(response.status === 403
        ? 'Action token expired. Refresh this page.'
        : 'Action rejected. Stop an active scan before deleting it or its points.');
      button.blur();
      await refreshSaved();
      message.textContent = 'Saved locally. No BACnet device was changed.';
    } catch (error) {
      message.textContent = error.message;
    } finally {
      localForm.dataset.pending = 'false';
      button.disabled = false;
    }
  });

  poll();
})();
