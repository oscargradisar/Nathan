// Voice: Nathan reads answers aloud (speech synthesis, built into the browser) and can
// take a spoken request (speech recognition, where the browser offers it).
import { store } from "./core.js";

let speaking = store.get("voice", false);

export const voiceOn = () => speaking;
export function setVoice(on) { speaking = on; store.set("voice", on); if (!on) stop(); }

function plainText(md) {
  return String(md).replace(/```[\s\S]*?```/g, " (code block) ").replace(/[#*_`>|-]+/g, " ").replace(/\s+/g, " ").trim();
}

export function say(text, { force = false } = {}) {
  if ((!speaking && !force) || !("speechSynthesis" in window)) return;
  stop();
  const u = new SpeechSynthesisUtterance(plainText(text).slice(0, 1200));
  const voices = speechSynthesis.getVoices();
  u.voice = voices.find((v) => /en-GB/i.test(v.lang) && /male|ryan|george|guy/i.test(v.name))
    || voices.find((v) => /en-GB/i.test(v.lang)) || voices.find((v) => /^en/i.test(v.lang)) || null;
  u.rate = 1.04; u.pitch = 0.95;
  speechSynthesis.speak(u);
}
export function stop() { if ("speechSynthesis" in window) speechSynthesis.cancel(); }

export const canListen = () => !!(window.SpeechRecognition || window.webkitSpeechRecognition);

// Listen once; calls onText(finalText). Returns a stop function, or null if unsupported.
export function listen(onText, onEnd) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return null;
  const rec = new SR();
  rec.lang = navigator.language || "en-US";
  rec.interimResults = false;
  rec.maxAlternatives = 1;
  rec.onresult = (e) => onText(e.results[0][0].transcript);
  rec.onend = () => onEnd && onEnd();
  rec.onerror = () => onEnd && onEnd();
  rec.start();
  return () => rec.stop();
}
