export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname !== '/proxy') return new Response('Not found', { status: 404 });

    const target = url.searchParams.get('url');
    const referer = url.searchParams.get('referer') || 'https://officialmoviebox.com/';

    // 1. Pretend to be a browser: bring Referer, User-Agent, and forward Range
    const headers = new Headers();
    headers.set('Referer', referer);
    headers.set('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...');
    
    const range = request.headers.get('Range');
    if (range) headers.set('Range', range);

    const upstream = await fetch(target, { headers });

    // 2. Force Content-Type to be returned (critical)
    const out = new Headers();
    out.set('Content-Type', 'video/mp4'); 
    out.set('Accept-Ranges', 'bytes'); // Tell the browser segments are supported
    out.set('Access-Control-Allow-Origin', '*');
    
    // 3. Pass through Content-Length and Content-Range (for seeking)
    ['Content-Length', 'Content-Range'].forEach(h => {
      const v = upstream.headers.get(h);
      if (v) out.set(h, v);
    });

    // 4. Stream the body through directly, don't buffer it
    return new Response(upstream.body, {
      status: upstream.status === 206 ? 206 : 200,
      headers: out
    });
  }
};
