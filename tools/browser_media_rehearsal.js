/* Inject only into isolated rehearsal pages; never load in the public product. */
(() => {
  const state = window.__mediaRehearsal = {
    peers: [], sockets: [], reports: [], messages: [], getUserMediaCalls: 0,
    source: null, audioContext: null, timers: [],
  };
  const NativePeer = window.RTCPeerConnection;
  window.RTCPeerConnection = class extends NativePeer {
    constructor(config) {
      super(window.__rehearsalForceRelay ? { ...config, iceTransportPolicy: 'relay' } : config);
      state.peers.push(this);
    }
  };
  const NativeSocket = window.WebSocket;
  window.WebSocket = class extends NativeSocket {
    constructor(...args) {
      super(...args);
      state.sockets.push(this);
      this.addEventListener('message', event => {
        try {
          const message = JSON.parse(event.data);
          state.messages.push({ type: message.type, payload: message.payload });
          if (state.messages.length > 100) state.messages.shift();
        } catch { /* non-JSON frames are not part of this protocol */ }
      });
    }
  };
  const realFetch = window.fetch.bind(window);
  window.fetch = async (url, options) => {
    let report;
    if (String(url).includes('/api/telemetry/connection/')) {
      try { report = JSON.parse(options.body); } catch { report = {}; }
      state.reports.push({ payload: report, status: null });
      report = state.reports[state.reports.length - 1];
    }
    const response = await realFetch(url, options);
    if (report) report.status = response.status;
    return response;
  };
  navigator.mediaDevices.getUserMedia = async () => {
    state.getUserMediaCalls += 1;
    if (!state.source) throw new DOMException('Activate the synthetic rehearsal source first.', 'NotAllowedError');
    return new MediaStream(state.source.getTracks());
  };
  document.addEventListener('DOMContentLoaded', () => {
    void import('/static/events/js/call_room.js').then(({ CallRoomController }) => {
      const tick = CallRoomController.prototype._tickTimer;
      CallRoomController.prototype._tickTimer = function (...args) {
        window.__rehearsalController = this;
        return tick.apply(this, args);
      };
    });
    const button = document.createElement('button');
    button.id = 'activateRehearsalMedia';
    button.textContent = 'Activate synthetic rehearsal media';
    button.style.cssText = 'position:fixed;bottom:8px;left:8px;z-index:999999;padding:12px;background:#fff;color:#111';
    button.onclick = async () => {
      if (!state.source) {
        const canvas = document.createElement('canvas');
        canvas.width = 320;
        canvas.height = 180;
        const ctx = canvas.getContext('2d');
        let frame = 0;
        const paint = () => {
          ctx.fillStyle = frame % 2 ? '#1e293b' : '#22354f';
          ctx.fillRect(0, 0, 320, 180);
          ctx.fillStyle = '#38bdf8';
          ctx.fillRect((frame * 5) % 280, 50, 40, 40);
          ctx.fillStyle = '#fff';
          ctx.font = '16px sans-serif';
          ctx.fillText(`Synthetic rehearsal ${frame++}`, 12, 140);
        };
        paint();
        state.timers.push(setInterval(paint, 100));
        const video = canvas.captureStream(10);
        const audio = state.audioContext = new AudioContext();
        const oscillator = audio.createOscillator();
        const gain = audio.createGain();
        const destination = audio.createMediaStreamDestination();
        gain.gain.value = 0.002;
        oscillator.connect(gain).connect(destination);
        oscillator.start();
        state.source = new MediaStream([...video.getVideoTracks(), ...destination.stream.getAudioTracks()]);
      }
      await state.audioContext.resume();
      button.textContent = `Synthetic media ${state.audioContext.state}`;
    };
    document.body.append(button);
    // Silence the test locally while preserving audio packets across the peer link.
    state.timers.push(setInterval(() => {
      document.querySelectorAll('video').forEach(video => {
        video.muted = true;
        if (video.srcObject && video.paused) void video.play().catch(() => {});
      });
    }, 200));
  });
  state.cleanup = () => {
    state.timers.forEach(clearInterval);
    state.source?.getTracks().forEach(track => track.stop());
    state.peers.forEach(peer => peer.close());
    state.sockets.forEach(socket => socket.close());
    void state.audioContext?.close();
  };
})();
