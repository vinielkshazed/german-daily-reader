/* ==========================================================================
   每日德语精读 —— 前端逻辑（原生 JS，无依赖）
   数据：data/news.json    笔记：localStorage（按日期 id 分别保存）
   ========================================================================== */
(() => {
  'use strict';

  const DATA_URL = 'data/news.json';
  const LS = {
    note: (id) => `gdr:note:${id}`,
    checked: (id) => `gdr:checked:${id}`,
    speed: 'gdr:speed',
    hideMeaning: 'gdr:hideMeaning',
  };
  const TOKEN_RE = /\d+(?:[.,]\d+)*|\p{L}+(?:[-‐'’][\p{L}\p{N}]+)*/gu;

  /* ---------------- 小工具 ---------------- */
  const $ = (sel, root = document) => root.querySelector(sel);
  /** 安全地创建元素：所有文本都走 textContent，避免抓取内容中的 HTML 注入 */
  function h(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  const store = {
    get(k, d = null) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); return true; } catch { return false; } },
    del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
  };
  const fmtTime = (s) => {
    if (!isFinite(s) || s < 0) s = 0;
    const m = Math.floor(s / 60), sec = Math.floor(s % 60);
    return `${m}:${String(sec).padStart(2, '0')}`;
  };
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  let toastTimer;
  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 2000);
  }
  /** 把词典原形转成查词用的关键字：去掉冠词、“in + dem”只取第一个词 */
  function lookupKey(v) {
    let q = (v.lemma || v.word || '').trim();
    q = q.replace(/^(der|die|das)\s+/i, '').split(/\s*[+/,(（]\s*/)[0].trim();
    return q || v.word;
  }
  const dictLinks = (q) => {
    const e = encodeURIComponent(q);
    return [
      ['Leo', `https://dict.leo.org/chinesisch-deutsch/${e}`],
      ['dict.cc', `https://www.dict.cc/?s=${e}`],
      ['DWDS', `https://www.dwds.de/wb/${e}`],
    ];
  };

  /* ---------------- 状态 ---------------- */
  let items = [];
  let current = null;

  /* ---------------- 数据加载 ---------------- */
  async function load() {
    try {
      const res = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      items = (Array.isArray(data) ? data : data.items || [])
        .filter((i) => i && i.date && i.sentence)
        .sort((a, b) => (a.date < b.date ? 1 : -1));
      if (!items.length) throw new Error('news.json 中还没有任何文章');
      buildArchive();
      showFromHash();
      window.addEventListener('hashchange', showFromHash);
    } catch (err) {
      const state = $('#state');
      state.classList.add('error');
      state.replaceChildren(
        h('p', {}, '😵 数据加载失败：', err.message),
        h('p', { class: 'step-sub' },
          location.protocol === 'file:'
            ? '直接双击打开 HTML 无法读取 JSON，请在项目目录运行 “python3 -m http.server 8000”，然后访问 http://localhost:8000'
            : '请稍后刷新重试，或检查 GitHub Actions 是否已成功生成 data/news.json。'),
      );
    }
  }

  /* ---------------- 历史归档 ---------------- */
  function buildArchive() {
    const sel = $('#dateSelect');
    sel.replaceChildren(...items.map((it) => {
      const hasNote = !!store.get(LS.note(it.id || it.date));
      const done = !!store.get(LS.checked(it.id || it.date));
      const mark = done ? ' ✓' : hasNote ? ' ✎' : '';
      const title = (it.title || '').length > 22 ? it.title.slice(0, 22) + '…' : it.title || '';
      return h('option', { value: it.date }, `${it.date}${mark} · ${title}`);
    }));
  }
  function refreshArchiveLabel() {
    const idx = items.indexOf(current);
    const opt = $('#dateSelect').options[idx];
    if (!opt) return;
    const id = current.id || current.date;
    const mark = store.get(LS.checked(id)) ? ' ✓' : store.get(LS.note(id)) ? ' ✎' : '';
    opt.textContent = opt.textContent.replace(/^(\S+)( [✓✎])?/, `$1${mark}`);
  }
  function showFromHash() {
    const date = decodeURIComponent(location.hash.slice(1));
    const item = items.find((i) => i.date === date) || items[0];
    if (item !== current) render(item);
  }
  function go(item) {
    if (!item) return;
    if (item === items[0]) {
      history.pushState(null, '', location.pathname + location.search);
      render(item);
    } else {
      location.hash = item.date;
    }
  }

  /* ---------------- 主渲染 ---------------- */
  function render(item) {
    current = item;
    const idx = items.indexOf(item);
    $('#dateSelect').value = item.date;
    $('#prevBtn').disabled = idx >= items.length - 1;
    $('#nextBtn').disabled = idx <= 0;
    document.title = `${item.date} · 每日德语精读`;

    $('#state').hidden = true;
    $('#content').hidden = false;

    renderArticle(item);
    renderStep1(item);
    renderStep2(item);
    updatePrompt();
    setAudio(item);
    window.scrollTo({ top: 0 });
  }

  function renderArticle(item) {
    const src = item.source || {};
    $('#articleMeta').replaceChildren(...[
      h('span', { class: 'tag' }, src.level || '德语新闻'),
      h('span', {}, src.name || ''),
      h('span', {}, '·'),
      h('time', { datetime: item.date }, item.date),
      item.analysis?.engine === 'fallback' ? h('span', { class: 'tag gray' }, '自动解析（降级）') : null,
    ].filter(Boolean));
    $('#articleTitle').textContent = item.title || '';
    const link = $('#articleLink');
    link.href = item.url || '#';
    link.hidden = !item.url;

    // 定位精读句所在段落并高亮
    const paras = item.paragraphs || [];
    let pIdx = item.sentence_meta?.paragraph_index ?? -1;
    if (!(paras[pIdx] || '').includes(item.sentence)) pIdx = paras.findIndex((p) => p.includes(item.sentence));

    const body = $('#articleBody');
    body.replaceChildren(...paras.map((p, i) => {
      if (i !== pIdx) return h('p', {}, p);
      const at = p.indexOf(item.sentence);
      const mark = h('mark', {
        class: 'focus', title: '当日精读句 · 点击跳到精读区',
        onclick: () => $('#step1').scrollIntoView({ behavior: 'smooth' }),
      }, item.sentence);
      return h('p', {}, p.slice(0, at), mark, p.slice(at + item.sentence.length));
    }));
  }

  /* ---------------- 第一步 ---------------- */
  function renderStep1(item) {
    const id = item.id || item.date;
    const meta = item.sentence_meta || {};

    // 提示信息
    const notices = [];
    if (meta.meets_min_words === false) notices.push(`本篇没有超过 20 词的句子，已选取最长句（${meta.word_count} 词）。`);
    else if (meta.word_count) notices.push(`本句共 ${meta.word_count} 个词。`);
    if (item.analysis?.engine === 'fallback') notices.push('今日词表由免费翻译自动生成，释义可能不够准确，建议点击 Leo / DWDS 核对。');
    const n = $('#sentenceNotice');
    n.textContent = notices.join(' ');
    n.hidden = !notices.length;

    // 精读句：每个词可点击，定位到词卡
    const sentence = item.sentence || '';
    const frag = [];
    let last = 0;
    for (const m of sentence.matchAll(TOKEN_RE)) {
      if (m.index > last) frag.push(sentence.slice(last, m.index));
      frag.push(h('button', {
        class: 'tok', type: 'button', dataset: { key: m[0].toLowerCase() },
        onclick: () => focusCard(m[0].toLowerCase()),
      }, m[0]));
      last = m.index + m[0].length;
    }
    frag.push(sentence.slice(last));
    $('#focusSentence').replaceChildren(...frag);

    // 词卡
    const vocab = item.vocab_list || [];
    $('#vocabCount').textContent = `（${vocab.length} 个）`;
    $('#vocabGrid').replaceChildren(...vocab.map((v) => {
      const card = h('div', {
        class: 'vcard', dataset: { key: (v.word || '').toLowerCase() }, tabindex: '0',
        title: '隐藏释义模式下，点击卡片查看释义',
      },
        h('div', { class: 'w', lang: 'de' }, v.word),
        v.lemma && v.lemma !== v.word ? h('div', { class: 'lemma', lang: 'de' }, v.lemma) : null,
        v.pos ? h('span', { class: 'pos' }, v.pos) : null,
        h('div', { class: 'meaning' + (v.meaning ? '' : ' empty-meaning') }, v.meaning || '（暂无释义，请查词典）'),
        h('div', { class: 'links' }, dictLinks(lookupKey(v)).map(([name, url]) =>
          h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', onclick: (e) => e.stopPropagation() }, name))),
      );
      const toggle = () => { if (document.body.classList.contains('hide-meaning')) card.classList.toggle('revealed'); };
      card.addEventListener('click', toggle);
      card.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
      return card;
    }));

    // 笔记
    const ta = $('#myNote');
    ta.value = store.get(LS.note(id), '');
    updateNoteMeta(false);
  }

  function focusCard(key) {
    document.querySelectorAll('.focus-sentence .tok.active').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll(`.focus-sentence .tok[data-key="${CSS.escape(key)}"]`).forEach((t) => t.classList.add('active'));
    const card = $(`.vcard[data-key="${CSS.escape(key)}"]`);
    if (!card) return toast('词表中没有这个词');
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.remove('flash');
    void card.offsetWidth; // 重新触发动画
    card.classList.add('flash');
    setTimeout(() => card.classList.remove('flash'), 1400);
  }

  function initHideMeaning() {
    const cb = $('#hideMeaning');
    cb.checked = store.get(LS.hideMeaning) === '1';
    document.body.classList.toggle('hide-meaning', cb.checked);
    cb.addEventListener('change', () => {
      document.body.classList.toggle('hide-meaning', cb.checked);
      document.querySelectorAll('.vcard.revealed').forEach((c) => c.classList.remove('revealed'));
      store.set(LS.hideMeaning, cb.checked ? '1' : '0');
    });
  }

  function updateNoteMeta(justSaved) {
    const v = $('#myNote').value;
    $('#noteCount').textContent = v ? `${v.length} 字` : '';
    const st = $('#noteStatus');
    if (justSaved) {
      const t = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      st.textContent = `✓ 已自动保存 ${t}`;
      st.className = 'saved';
    } else {
      st.textContent = v ? '已从本浏览器恢复上次的笔记' : '内容自动保存在本浏览器（刷新不丢失）';
      st.className = '';
    }
  }

  function initNotes() {
    const ta = $('#myNote');
    const save = debounce(() => {
      if (!current) return;
      const id = current.id || current.date;
      const ok = ta.value.trim() ? store.set(LS.note(id), ta.value) : (store.del(LS.note(id)), true);
      if (ok) updateNoteMeta(true); else toast('⚠️ 浏览器禁止了本地存储，笔记无法保存');
      refreshArchiveLabel();
    }, 400);
    ta.addEventListener('input', () => {
      $('#noteStatus').textContent = '正在输入…';
      $('#noteStatus').className = '';
      $('#noteCount').textContent = ta.value ? `${ta.value.length} 字` : '';
      save();
      syncMine();
      updatePrompt();
    });
  }

  /* ---------------- 第二步 ---------------- */
  function renderStep2(item) {
    // 每次切换文章都重新折叠，避免“剧透”
    setAnswerOpen(false);
    $('#cmpRef').textContent = item.reference_translation || '（暂无参考译文）';

    const g = item.grammar_notes || {};
    const sk = $('#gSkeleton');
    sk.replaceChildren(h('b', {}, '句子主干'), g.skeleton || '（暂无）');
    sk.hidden = !g.skeleton;

    $('#gStructure').replaceChildren(...(g.structure || []).map((s) => h('li', {},
      h('div', { class: 'seg-text', lang: 'de' }, s.text),
      s.type ? h('span', { class: 'seg-type' }, s.type) : null,
      s.explanation ? h('div', { class: 'seg-exp' }, s.explanation) : null,
    )));
    $('#gPoints').replaceChildren(...(g.points || []).map((p) =>
      h('li', {}, h('b', {}, p.title), p.explanation ? `：${p.explanation}` : '')));

    const engine = item.analysis?.engine;
    $('#gNote').textContent = [
      g.note,
      engine ? `解析生成方式：${engine === 'fallback' ? '规则 + 免费机器翻译' : engine}` : '',
    ].filter(Boolean).join('  ·  ');
  }

  function syncMine() {
    const v = $('#myNote').value.trim();
    const box = $('#cmpMine');
    box.textContent = v || '（你还没有写翻译。可以回到第一步先写下自己的理解，再来对比。）';
    box.classList.toggle('placeholder', !v);
  }

  function setAnswerOpen(open) {
    $('#answerPanel').hidden = !open;
    const btn = $('#revealBtn');
    btn.setAttribute('aria-expanded', String(open));
    btn.textContent = open ? '🙈 收起参考解析' : '✅ 完成精读，核对参考解析';
  }

  function initReveal() {
    $('#revealBtn').addEventListener('click', () => {
      const open = $('#answerPanel').hidden;
      if (open && !$('#myNote').value.trim()
          && !confirm('你还没有写自己的翻译。\n先自己试一试效果更好，确定直接查看答案吗？')) {
        $('#myNote').focus();
        return;
      }
      syncMine();
      setAnswerOpen(open);
      if (open && current) {
        store.set(LS.checked(current.id || current.date), '1');
        refreshArchiveLabel();
        $('#answerPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  }

  /* ---------------- 第三步 ---------------- */
  function buildPrompt() {
    if (!current) return '';
    const mine = $('#myNote').value.trim();
    return [
      '我正在用新闻精读的方式学习德语（约 A2–B1 水平）。下面是一句德语新闻原句和我自己写的中文翻译，请你像德语老师一样帮我批改：',
      '1. 逐条指出我的翻译中理解错误、遗漏或不够准确的地方，并说明对应的德语成分；',
      '2. 讲解这句话涉及的关键语法（语序、格位、时态/语态、从句结构等）；',
      '3. 给出一个准确、自然的参考译文；',
      '4. 挑出 3 个最值得记住的单词或固定搭配，并各给一个简单例句。',
      '',
      `【新闻标题】${current.title || ''}`,
      `【德语原句】${current.sentence}`,
      `【我的翻译】${mine || '（我还没有写出翻译，请先帮我逐词拆解这句话，再给出译文。）'}`,
    ].join('\n');
  }
  function updatePrompt() { $('#promptPreview').textContent = buildPrompt(); }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // 兼容非 HTTPS / 老浏览器
      const ta = h('textarea', { style: 'position:fixed;top:-1000px;opacity:0' });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch { ok = false; }
      ta.remove();
      return ok;
    }
  }
  function initCopy() {
    const btn = $('#copyPromptBtn');
    btn.addEventListener('click', async () => {
      const ok = await copyText(buildPrompt());
      if (ok) {
        btn.textContent = '✅ 已复制！粘贴到 AI 聊天框即可';
        toast('提示词已复制到剪贴板');
      } else {
        $('.prompt-preview').open = true;
        toast('复制失败，请手动选择下方文本复制');
      }
      setTimeout(() => { btn.textContent = '📋 一键复制对比提示词'; }, 2500);
    });
  }

  /* ---------------- 音频播放器 ---------------- */
  const audio = $('#audio');
  const player = $('#player');
  const seek = $('#seek');
  let seeking = false;

  function setAudio(item) {
    audio.pause();
    player.classList.remove('playing');
    seek.value = 0;
    seek.max = 0;
    $('#curTime').textContent = '0:00';
    $('#durTime').textContent = item.audio?.duration || '0:00';
    const msg = $('#playerMsg');
    msg.hidden = true;

    const url = item.audio?.url;
    if (!url) {
      audio.removeAttribute('src');
      audio.load();
      player.classList.add('disabled');
      msg.replaceChildren('本篇没有音频。', item.url ? h('a', { href: item.url, target: '_blank', rel: 'noopener noreferrer' }, '去原文页面看看 ↗') : '');
      msg.hidden = false;
      return;
    }
    player.classList.remove('disabled');
    audio.src = url.replace(/^http:\/\//, 'https://'); // GitHub Pages 是 HTTPS，避免混合内容被拦截
    applyRate(parseFloat(store.get(LS.speed, '1')) || 1);
  }

  function applyRate(rate) {
    audio.defaultPlaybackRate = rate;
    audio.playbackRate = rate;
    document.querySelectorAll('.speed button').forEach((b) =>
      b.classList.toggle('active', parseFloat(b.dataset.rate) === rate));
  }

  function togglePlay() {
    if (player.classList.contains('disabled')) return;
    if (audio.paused) {
      audio.play().catch((e) => toast(`无法播放：${e.message}`));
    } else {
      audio.pause();
    }
  }
  function skip(sec) {
    if (!isFinite(audio.duration)) return;
    audio.currentTime = Math.min(Math.max(0, audio.currentTime + sec), audio.duration);
  }

  function initPlayer() {
    $('#playBtn').addEventListener('click', togglePlay);
    $('#backBtn').addEventListener('click', () => skip(-5));
    document.querySelectorAll('.speed button').forEach((b) => b.addEventListener('click', () => {
      const r = parseFloat(b.dataset.rate);
      applyRate(r);
      store.set(LS.speed, String(r));
    }));

    audio.addEventListener('play', () => { player.classList.add('playing'); $('#playBtn').setAttribute('aria-label', '暂停'); });
    audio.addEventListener('pause', () => { player.classList.remove('playing'); $('#playBtn').setAttribute('aria-label', '播放'); });
    audio.addEventListener('loadedmetadata', () => {
      seek.max = audio.duration || 0;
      $('#durTime').textContent = fmtTime(audio.duration);
      audio.playbackRate = audio.defaultPlaybackRate;
    });
    audio.addEventListener('timeupdate', () => {
      if (seeking) return;
      seek.value = audio.currentTime;
      $('#curTime').textContent = fmtTime(audio.currentTime);
    });
    audio.addEventListener('error', () => {
      if (!audio.getAttribute('src')) return;
      player.classList.add('disabled');
      const msg = $('#playerMsg');
      msg.replaceChildren('音频加载失败（可能已下线或禁止外链）。',
        current?.url ? h('a', { href: current.url, target: '_blank', rel: 'noopener noreferrer' }, '在原文页面收听 ↗') : '');
      msg.hidden = false;
    });

    seek.addEventListener('input', () => { seeking = true; $('#curTime').textContent = fmtTime(+seek.value); });
    seek.addEventListener('change', () => { audio.currentTime = +seek.value; seeking = false; });

    // 键盘快捷键（输入框内不触发）
    document.addEventListener('keydown', (e) => {
      const tag = (e.target.tagName || '').toLowerCase();
      if (['textarea', 'input', 'select'].includes(tag) || e.target.isContentEditable) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.code === 'Space' && tag !== 'button') { e.preventDefault(); togglePlay(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); skip(-5); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); skip(5); }
    });

    // 锁屏/通知栏控制（支持的浏览器）
    if ('mediaSession' in navigator) {
      navigator.mediaSession.setActionHandler('play', () => audio.play());
      navigator.mediaSession.setActionHandler('pause', () => audio.pause());
      navigator.mediaSession.setActionHandler('seekbackward', () => skip(-5));
      navigator.mediaSession.setActionHandler('seekforward', () => skip(5));
      audio.addEventListener('play', () => {
        if (current && window.MediaMetadata) {
          navigator.mediaSession.metadata = new MediaMetadata({
            title: current.title, artist: current.source?.name || '', album: '每日德语精读',
          });
        }
      });
    }
  }

  /* ---------------- 归档导航 ---------------- */
  function initArchiveNav() {
    $('#dateSelect').addEventListener('change', (e) => go(items.find((i) => i.date === e.target.value)));
    $('#prevBtn').addEventListener('click', () => go(items[items.indexOf(current) + 1]));
    $('#nextBtn').addEventListener('click', () => go(items[items.indexOf(current) - 1]));
  }

  /* ---------------- 启动 ---------------- */
  initPlayer();
  initHideMeaning();
  initNotes();
  initReveal();
  initCopy();
  initArchiveNav();
  load();
})();
