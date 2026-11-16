/* MedGraph Flow - Local Persistence, File System Sync & GitHub Gist Cloud Sync */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.MedSync = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const PROGRESS_KEY = 'medgraph.progress.v1';
  const SETTINGS_KEY = 'medgraph.sync_settings.v1';
  const GRAPH_CACHE_KEY = 'medgraph.graph_override.v1';
  const GIST_FILENAME = 'medgraph_progress.json';

  function mergeProgress(localProg, remoteProg) {
    if (!remoteProg || typeof remoteProg !== 'object' || remoteProg.schema_version !== 1) {
      return localProg;
    }
    if (!localProg || typeof localProg !== 'object' || localProg.schema_version !== 1) {
      return remoteProg;
    }

    const mergedStates = { ...(localProg.node_states || {}) };
    for (const [nid, rState] of Object.entries(remoteProg.node_states || {})) {
      const lState = mergedStates[nid];
      if (!lState) {
        mergedStates[nid] = rState;
        continue;
      }
      const lTime = Date.parse(lState.last_reviewed_at || '') || 0;
      const rTime = Date.parse(rState.last_reviewed_at || '') || 0;
      if (rTime > lTime) {
        mergedStates[nid] = rState;
      } else if (rTime === lTime && (rState.review_count || 0) > (lState.review_count || 0)) {
        mergedStates[nid] = rState;
      }
    }

    const lSessTime = Date.parse(localProg.session?.last_active_at || localProg.updated_at || '') || 0;
    const rSessTime = Date.parse(remoteProg.session?.last_active_at || remoteProg.updated_at || '') || 0;
    const chosenSession = rSessTime > lSessTime ? remoteProg.session : localProg.session;
    const maxUpdated = new Date(Math.max(lSessTime, rSessTime, Date.parse(localProg.updated_at || '') || 0, Date.parse(remoteProg.updated_at || '') || 0) || Date.now()).toISOString();

    return {
      schema_version: 1,
      updated_at: maxUpdated,
      node_states: mergedStates,
      session: chosenSession,
    };
  }

  function loadSettings() {
    try {
      const raw = localStorage.getItem(SETTINGS_KEY);
      return raw ? JSON.parse(raw) : { githubToken: '', gistId: '', autoSync: true, remoteGraphUrl: '' };
    } catch {
      return { githubToken: '', gistId: '', autoSync: true, remoteGraphUrl: '' };
    }
  }

  function saveSettings(settings) {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    } catch {
      // Ignore quota errors
    }
  }

  function loadLocalProgress(createFallback) {
    try {
      const raw = localStorage.getItem(PROGRESS_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && parsed.schema_version === 1) return parsed;
      }
    } catch {
      // Fall back to fresh progress
    }
    return createFallback();
  }

  function saveLocalProgress(progress) {
    try {
      localStorage.setItem(PROGRESS_KEY, JSON.stringify(progress));
    } catch {
      // Ignore storage failures in restricted contexts
    }
  }

  function loadCachedGraphOverride() {
    try {
      const raw = localStorage.getItem(GRAPH_CACHE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function saveCachedGraphOverride(graph) {
    try {
      localStorage.setItem(GRAPH_CACHE_KEY, JSON.stringify(graph));
    } catch {
      // Ignore storage quota overflow for large graphs
    }
  }

  async function githubRequest(url, token, options = {}) {
    const res = await fetch(url, {
      ...options,
      headers: {
        Accept: 'application/vnd.github+json',
        Authorization: `Bearer ${token}`,
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.headers || {}),
      },
    });
    if (!res.ok) {
      const msg = await res.text().catch(() => '');
      throw new Error(`GitHub API ${res.status}: ${msg.slice(0, 160)}`);
    }
    return res.json();
  }

  async function createPrivateGist(token, initialProgress) {
    const data = await githubRequest('https://api.github.com/gists', token, {
      method: 'POST',
      body: JSON.stringify({
        description: 'MedGraph Flow - Fudan Medical Mastery & Breakpoint Progress',
        public: false,
        files: {
          [GIST_FILENAME]: {
            content: JSON.stringify(initialProgress, null, 2),
          },
        },
      }),
    });
    return data.id;
  }

  async function syncWithGist(settings, localProgress) {
    const token = (settings.githubToken || '').trim();
    if (!token) throw new Error('请先填写 GitHub Personal Access Token (gist 权限)。');

    let gistId = (settings.gistId || '').trim();
    if (!gistId) {
      gistId = await createPrivateGist(token, localProgress);
      return { gistId, progress: localProgress, action: 'created' };
    }

    const gist = await githubRequest(`https://api.github.com/gists/${encodeURIComponent(gistId)}`, token);
    const fileObj = gist.files && gist.files[GIST_FILENAME];
    let remoteProg = null;
    if (fileObj && fileObj.content) {
      try {
        remoteProg = JSON.parse(fileObj.content);
      } catch {
        remoteProg = null;
      }
    }

    const merged = mergeProgress(localProgress, remoteProg);
    await githubRequest(`https://api.github.com/gists/${encodeURIComponent(gistId)}`, token, {
      method: 'PATCH',
      body: JSON.stringify({
        files: {
          [GIST_FILENAME]: {
            content: JSON.stringify(merged, null, 2),
          },
        },
      }),
    });

    return { gistId, progress: merged, action: 'synced' };
  }

  async function fetchRemoteGraph(url, token = '') {
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    const res = await fetch(url, { headers, cache: 'no-store' });
    if (!res.ok) throw new Error(`拉取图谱失败 HTTP ${res.status}`);
    const data = await res.json();
    if (!data || data.schema_version !== 1 || !Array.isArray(data.nodes)) {
      throw new Error('远端 JSON 不是有效的 MedGraph 图谱格式');
    }
    return data;
  }

  return {
    PROGRESS_KEY,
    SETTINGS_KEY,
    GIST_FILENAME,
    mergeProgress,
    loadSettings,
    saveSettings,
    loadLocalProgress,
    saveLocalProgress,
    loadCachedGraphOverride,
    saveCachedGraphOverride,
    createPrivateGist,
    syncWithGist,
    fetchRemoteGraph,
  };
});
