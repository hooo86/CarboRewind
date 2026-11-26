import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./i18n";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/plus-jakarta-sans/600.css";
import "@fontsource/plus-jakarta-sans/700.css";
import "@fontsource/jetbrains-mono/500.css";
import "./styles.css";
import { applyTheme, savedTheme } from "./theme";

applyTheme(savedTheme());

const root = document.getElementById("root");
if (!root) throw new Error("#root element missing");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
