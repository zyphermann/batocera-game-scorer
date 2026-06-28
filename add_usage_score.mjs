import fs from "node:fs";

const inputPath = new URL("./1782635849.csv", import.meta.url);
const outputPath = new URL("./1782635849_with_usage_score.csv", import.meta.url);

const PLAYCOUNT_WEIGHT = 0.65;
const GAMETIME_WEIGHT = 0.35;

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];

    if (inQuotes) {
      if (char === "\"") {
        if (text[i + 1] === "\"") {
          field += "\"";
          i += 1;
        } else {
          inQuotes = false;
        }
      } else {
        field += char;
      }
      continue;
    }

    if (char === "\"") {
      inQuotes = true;
    } else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else if (char !== "\r") {
      field += char;
    }
  }

  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }

  return rows;
}

function formatCsvValue(value) {
  const text = String(value ?? "");
  if (/[",\n\r]/.test(text)) {
    return `"${text.replaceAll("\"", "\"\"")}"`;
  }
  return text;
}

function toNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

const csvText = fs.readFileSync(inputPath, "utf8");
const rows = parseCsv(csvText);
const header = rows[0];
const dataRows = rows.slice(1);
const col = Object.fromEntries(header.map((name, index) => [name, index]));

const gameRows = dataRows.filter((row) => row[col.title] && row[col.system]);
const maxPlaycount = Math.max(...gameRows.map((row) => toNumber(row[col.playcount]) ?? 0));
const maxGametimeHours = Math.max(...gameRows.map((row) => toNumber(row[col.gametime_hours]) ?? 0));

function logNormalized(value, maxValue) {
  if (!maxValue || value === null || value <= 0) {
    return 0;
  }
  return Math.log1p(value) / Math.log1p(maxValue);
}

const outputHeader = [...header, "usage_score"];
const outputRows = dataRows.map((row) => {
  if (!row[col.title] || !row[col.system]) {
    return [...row, ""];
  }

  const playcountScore = logNormalized(toNumber(row[col.playcount]), maxPlaycount);
  const gametimeScore = logNormalized(toNumber(row[col.gametime_hours]), maxGametimeHours);
  const usageScore = 100 * (
    PLAYCOUNT_WEIGHT * playcountScore +
    GAMETIME_WEIGHT * gametimeScore
  );

  return [...row, usageScore.toFixed(2)];
});

const outputText = [outputHeader, ...outputRows]
  .map((row) => row.map(formatCsvValue).join(","))
  .join("\n");

fs.writeFileSync(outputPath, `${outputText}\n`);

console.log(`Wrote ${outputPath.pathname}`);
console.log(`Game rows scored: ${gameRows.length}`);
console.log(`Formula: usage_score = 100 * (${PLAYCOUNT_WEIGHT} * log_norm(playcount) + ${GAMETIME_WEIGHT} * log_norm(gametime_hours))`);
