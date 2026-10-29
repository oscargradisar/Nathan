// Paul Morphy (1837–1884), Nathan's patron saint. Public-domain photographs from
// Wikimedia Commons (see /static/img/CREDITS.json); the captions rotate.
import { $, pick } from "./core.js";

const PORTRAITS = ["morphy-portrait.jpg", "morphy-daguerreotype.jpg", "morphy-1859.jpg"];
const LINES = [
  "beat the best players in Europe at 21, then retired.",
  "played eight games at once, blindfolded. Your models need a backup at 20 seconds.",
  "unofficial world champion, never officially rated. Elo hadn't been invented; neither had excuses.",
  "won the Opera Game in 17 moves while two aristocrats consulted each other. Teamwork has limits.",
  "offered rivals a pawn and the first move. Nathan offers its models a 60-second timeout.",
  "considered the most brilliant player of his age. The age was 21.",
];

export function initMorphy() {
  let i = 0;
  setInterval(() => {
    i = (i + 1) % PORTRAITS.length;
    const holo = $("holo"), img = $("patron-img");
    holo.classList.add("swap");
    img.style.opacity = "0";
    setTimeout(() => {
      holo.src = `/static/img/${PORTRAITS[i]}`;
      img.src = `/static/img/${PORTRAITS[(i + 1) % PORTRAITS.length]}`;
      $("patron-line").textContent = pick(LINES);
      holo.classList.remove("swap");
      img.style.opacity = "1";
    }, 700);
  }, 24000);
}
