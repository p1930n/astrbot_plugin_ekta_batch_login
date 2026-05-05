const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { fileURLToPath } = require("url");

const BASE_URL = "https://ekta.qdgw.edu.cn/api/app/client/v1/";
const APP_AES_KEY = Buffer.from("qdgwx&Client2129", "utf8");
const OSS_AES_KEY = Buffer.from("BjwlX#Client2022", "utf8");
const DEFAULT_DEVICE = "FAAE6E21-013F-42D1-B722-599DE3DC340F";
const ACTIVITY_QR_SOURCE = "schActivityCode@Xj";
const DEFAULT_DELAY_MS = 1000;
const DEFAULT_MAX_ACCOUNTS = 80;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const MAX_QR_DECODE_ATTEMPTS = 60;
const MAX_DARK_CANDIDATE_BOXES = 16;
const QR_TARGET_SIDE = 720;

function loadDependency(name) {
  try {
    return require(name);
  } catch (_) {
    const fallback = path.resolve(__dirname, "../../../ekta_batch_login/node_modules", name);
    try {
      return require(fallback);
    } catch (_) {
      throw new Error(`缺少 Node 依赖 ${name}，请在插件 vendor 目录运行 npm install --omit=dev`);
    }
  }
}

function encryptText(text) {
  const cipher = crypto.createCipheriv("aes-128-ecb", APP_AES_KEY, null);
  cipher.setAutoPadding(true);
  return Buffer.concat([cipher.update(String(text), "utf8"), cipher.final()]).toString("base64");
}

function decryptWithKey(text, key) {
  const decipher = crypto.createDecipheriv("aes-128-ecb", key, null);
  decipher.setAutoPadding(true);
  return Buffer.concat([decipher.update(String(text).replace(/ /g, "+"), "base64"), decipher.final()]).toString("utf8");
}

function decryptAppText(text) {
  return decryptWithKey(text, APP_AES_KEY);
}

function decryptOssText(text) {
  return decryptWithKey(text, OSS_AES_KEY);
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;

  text = text.replace(/^\uFEFF/, "");
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    const next = text[i + 1];

    if (quoted) {
      if (ch === '"' && next === '"') {
        field += '"';
        i += 1;
      } else if (ch === '"') {
        quoted = false;
      } else {
        field += ch;
      }
      continue;
    }

    if (ch === '"') {
      quoted = true;
    } else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else if (ch !== "\r") {
      field += ch;
    }
  }

  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }

  const headers = (rows.shift() || []).map((name) => name.trim());
  return rows
    .filter((cells) => cells.some((cell) => cell.trim() !== ""))
    .map((cells, index) => {
      const item = { _line: index + 2 };
      headers.forEach((header, i) => {
        item[header] = (cells[i] || "").trim();
      });
      return item;
    });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function readArgs(argv) {
  const options = {
    accountsPath: "",
    images: [],
    schoolCode: "",
    delayMs: DEFAULT_DELAY_MS,
    maxAccounts: DEFAULT_MAX_ACCOUNTS,
    dryRun: false,
    decodeOnly: false,
    jsonl: false,
    taskJson: "",
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const readValue = () => {
      const value = argv[i + 1];
      i += 1;
      return value;
    };

    if (arg === "--accounts") options.accountsPath = readValue();
    else if (arg.startsWith("--accounts=")) options.accountsPath = arg.slice("--accounts=".length);
    else if (arg === "--image") options.images.push(readValue());
    else if (arg.startsWith("--image=")) options.images.push(arg.slice("--image=".length));
    else if (arg === "--school-code") options.schoolCode = readValue();
    else if (arg.startsWith("--school-code=")) options.schoolCode = arg.slice("--school-code=".length);
    else if (arg === "--delay-ms") options.delayMs = Number(readValue());
    else if (arg.startsWith("--delay-ms=")) options.delayMs = Number(arg.slice("--delay-ms=".length));
    else if (arg === "--max-accounts") options.maxAccounts = Number(readValue());
    else if (arg.startsWith("--max-accounts=")) options.maxAccounts = Number(arg.slice("--max-accounts=".length));
    else if (arg === "--dry-run") options.dryRun = true;
    else if (arg === "--decode-only") options.decodeOnly = true;
    else if (arg === "--jsonl") options.jsonl = true;
    else if (arg === "--task-json") options.taskJson = readValue();
    else if (arg.startsWith("--task-json=")) options.taskJson = arg.slice("--task-json=".length);
    else throw new Error(`未知参数: ${arg}`);
  }

  if (!options.accountsPath && !options.decodeOnly) {
    throw new Error("缺少 --accounts");
  }
  if (options.images.length === 0 && !options.taskJson) {
    throw new Error("缺少 --image");
  }
  if (!Number.isFinite(options.delayMs) || options.delayMs < 0) {
    throw new Error("--delay-ms 必须是非负数字");
  }
  if (!Number.isFinite(options.maxAccounts) || options.maxAccounts < 1) {
    throw new Error("--max-accounts 必须是正整数");
  }
  if (options.accountsPath) {
    options.accountsPath = path.resolve(options.accountsPath);
  }
  return options;
}

async function apiRequest(method, apiPath, params = {}, token = "") {
  const authorization = encryptText(
    JSON.stringify({
      token,
      platform: 3,
      version: "2.0.5",
      device: DEFAULT_DEVICE,
      timestamp: Date.now(),
    }),
  );

  const url = new URL(apiPath, BASE_URL);
  const request = {
    method,
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Authorization: authorization,
    },
  };

  if (Object.keys(params).length > 0) {
    const encryptedParams = encodeURI(encryptText(JSON.stringify(params))).replace(/\+/g, "%2B");
    if (method === "GET") {
      url.searchParams.set("params", encryptedParams);
    } else {
      request.body = new URLSearchParams({ params: encryptedParams }).toString();
    }
  }

  const response = await fetch(url, request);
  const text = await response.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch (_) {
    body = JSON.parse(decryptAppText(text));
  }
  return { httpStatus: response.status, body };
}

async function apiGet(apiPath, params = {}, token = "") {
  return apiRequest("GET", apiPath, params, token);
}

async function apiPost(apiPath, params = {}, token = "") {
  return apiRequest("POST", apiPath, params, token);
}

async function getDefaultSchoolCode() {
  const res = await apiGet("common/all-school");
  if (res.body.code !== 0 || !Array.isArray(res.body.data) || res.body.data.length === 0) {
    throw new Error(`无法读取学校列表: ${res.body.msg || res.body.code}`);
  }
  const school = res.body.data[0];
  return { schoolCode: String(school.id), schoolName: school.name || "" };
}

async function loginAccount(account, fallbackSchoolCode) {
  const code = account.code || account.username || account.account;
  const password = account.password || account.passwd || account.pwd;
  const schoolCode = account.schoolCode || fallbackSchoolCode;

  if (!code || !password) {
    return {
      error: {
        ok: false,
        codeStatus: "CSV_ERROR",
        message: `第 ${account._line} 行缺少 code 或 password`,
      },
    };
  }

  const login = await apiGet("token", { schoolCode, code, password });
  if (login.body.code !== 0) {
    return {
      error: {
        ok: false,
        codeStatus: login.body.code,
        message: login.body.msg || "登录失败",
      },
    };
  }

  const token = login.body.data && login.body.data.access_token;
  const info = token ? await apiGet("student/user/my-info", {}, token) : null;
  const user = info && info.body.code === 0 ? info.body.data : {};
  return { code, schoolCode, token, user };
}

async function readImageBuffer(source) {
  if (/^https?:\/\//i.test(source)) {
    const response = await fetch(source);
    if (!response.ok) {
      throw new Error(`图片下载失败: HTTP ${response.status}`);
    }
    return Buffer.from(await response.arrayBuffer());
  }
  if (/^file:\/\//i.test(source)) {
    return fs.readFileSync(fileURLToPath(source));
  }
  return fs.readFileSync(source);
}

async function decodeImageQr(source) {
  const jsQR = loadDependency("jsqr");
  const buffer = await readImageBuffer(source);
  const image = decodeImage(buffer);
  const qr = scanQrImage(jsQR, image);
  if (!qr) {
    throw new Error("没有识别到二维码");
  }
  return qr.data;
}

function scanQrImage(jsQR, image) {
  const boxes = candidateQrBoxes(image);
  let attempts = 0;

  for (const box of boxes) {
    const cropped = cropImage(image, box);
    const variants = imageVariants(cropped);
    for (const variant of variants) {
      attempts += 1;
      const qr = jsQR(variant.data, variant.width, variant.height, {
        inversionAttempts: "attemptBoth",
      });
      if (qr) {
        return qr;
      }
      if (attempts >= MAX_QR_DECODE_ATTEMPTS) {
        return null;
      }
    }
  }
  return null;
}

function candidateQrBoxes(image) {
  const boxes = [{ x: 0, y: 0, width: image.width, height: image.height }];
  boxes.push(...centerSquareBoxes(image));
  boxes.push(...darkSquareBoxes(image));
  return dedupeBoxes(boxes, image.width, image.height);
}

function centerSquareBoxes(image) {
  const boxes = [];
  const baseSide = Math.min(image.width, image.height);
  const sideScales = [0.28, 0.38, 0.52, 0.72, 0.9];
  const centerYScales = [0.35, 0.45, 0.55, 0.65, 0.75];
  for (const sideScale of sideScales) {
    const side = Math.round(baseSide * sideScale);
    for (const centerYScale of centerYScales) {
      boxes.push(squareBox(image.width / 2, image.height * centerYScale, side, image.width, image.height));
    }
  }
  return boxes;
}

function darkSquareBoxes(image) {
  const width = image.width;
  const height = image.height;
  const minSide = Math.max(80, Math.round(Math.min(width, height) * 0.12));
  const maxSide = Math.max(minSide, Math.round(Math.min(width, height) * 0.48));
  const integral = buildDarkIntegral(image);
  const candidates = [];

  for (let side = minSide; side <= maxSide; side = Math.round(side * 1.32) + 1) {
    const step = Math.max(16, Math.round(side / 4));
    for (let y = 0; y <= height - side; y += step) {
      for (let x = 0; x <= width - side; x += step) {
        const darkCount = integralDarkCount(integral, width, x, y, side, side);
        const density = darkCount / (side * side);
        if (density < 0.04 || density > 0.55) {
          continue;
        }
        const centerBonus = 1 - Math.min(0.35, Math.abs(x + side / 2 - width / 2) / width);
        candidates.push({
          box: expandBox({ x, y, width: side, height: side }, Math.round(side * 0.22), width, height),
          score: density * centerBonus,
        });
      }
    }
  }

  candidates.sort((left, right) => right.score - left.score);
  const boxes = [];
  for (const candidate of candidates) {
    if (boxes.some((box) => boxOverlapRatio(box, candidate.box) > 0.55)) {
      continue;
    }
    boxes.push(candidate.box);
    if (boxes.length >= MAX_DARK_CANDIDATE_BOXES) {
      break;
    }
  }
  return boxes;
}

function imageVariants(image) {
  const variants = [image, thresholdImage(image, 150), thresholdImage(image, 190)];
  const maxSide = Math.max(image.width, image.height);
  if (maxSide < QR_TARGET_SIDE) {
    const scale = Math.max(2, Math.min(4, Math.ceil(QR_TARGET_SIDE / maxSide)));
    const scaled = scaleImageNearest(image, scale);
    variants.push(scaled, thresholdImage(scaled, 150), thresholdImage(scaled, 190));
  }
  return variants;
}

function buildDarkIntegral(image) {
  const width = image.width;
  const height = image.height;
  const integral = new Uint32Array((width + 1) * (height + 1));
  for (let y = 1; y <= height; y += 1) {
    let rowSum = 0;
    for (let x = 1; x <= width; x += 1) {
      const index = ((y - 1) * width + (x - 1)) * 4;
      const alpha = image.data[index + 3];
      const luminance = (
        image.data[index] * 299
        + image.data[index + 1] * 587
        + image.data[index + 2] * 114
      ) / 1000;
      rowSum += alpha > 32 && luminance < 128 ? 1 : 0;
      const integralIndex = y * (width + 1) + x;
      integral[integralIndex] = integral[integralIndex - width - 1] + rowSum;
    }
  }
  return integral;
}

function integralDarkCount(integral, imageWidth, x, y, width, height) {
  const stride = imageWidth + 1;
  const left = x;
  const top = y;
  const right = x + width;
  const bottom = y + height;
  return (
    integral[bottom * stride + right]
    - integral[top * stride + right]
    - integral[bottom * stride + left]
    + integral[top * stride + left]
  );
}

function cropImage(image, box) {
  const data = new Uint8ClampedArray(box.width * box.height * 4);
  for (let y = 0; y < box.height; y += 1) {
    const sourceStart = ((box.y + y) * image.width + box.x) * 4;
    const sourceEnd = sourceStart + box.width * 4;
    data.set(image.data.subarray(sourceStart, sourceEnd), y * box.width * 4);
  }
  return { data, width: box.width, height: box.height };
}

function thresholdImage(image, threshold) {
  const data = new Uint8ClampedArray(image.data.length);
  for (let i = 0; i < image.data.length; i += 4) {
    const alpha = image.data[i + 3];
    const luminance = (
      image.data[i] * 299
      + image.data[i + 1] * 587
      + image.data[i + 2] * 114
    ) / 1000;
    const value = alpha > 32 && luminance < threshold ? 0 : 255;
    data[i] = value;
    data[i + 1] = value;
    data[i + 2] = value;
    data[i + 3] = 255;
  }
  return { data, width: image.width, height: image.height };
}

function scaleImageNearest(image, scale) {
  const width = image.width * scale;
  const height = image.height * scale;
  const data = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y += 1) {
    const sourceY = Math.floor(y / scale);
    for (let x = 0; x < width; x += 1) {
      const sourceX = Math.floor(x / scale);
      const sourceIndex = (sourceY * image.width + sourceX) * 4;
      const targetIndex = (y * width + x) * 4;
      data[targetIndex] = image.data[sourceIndex];
      data[targetIndex + 1] = image.data[sourceIndex + 1];
      data[targetIndex + 2] = image.data[sourceIndex + 2];
      data[targetIndex + 3] = image.data[sourceIndex + 3];
    }
  }
  return { data, width, height };
}

function squareBox(centerX, centerY, side, imageWidth, imageHeight) {
  return clampBox({
    x: Math.round(centerX - side / 2),
    y: Math.round(centerY - side / 2),
    width: side,
    height: side,
  }, imageWidth, imageHeight);
}

function expandBox(box, padding, imageWidth, imageHeight) {
  return clampBox({
    x: box.x - padding,
    y: box.y - padding,
    width: box.width + padding * 2,
    height: box.height + padding * 2,
  }, imageWidth, imageHeight);
}

function clampBox(box, imageWidth, imageHeight) {
  const x = Math.max(0, Math.min(imageWidth - 1, box.x));
  const y = Math.max(0, Math.min(imageHeight - 1, box.y));
  const right = Math.max(x + 1, Math.min(imageWidth, box.x + box.width));
  const bottom = Math.max(y + 1, Math.min(imageHeight, box.y + box.height));
  return {
    x,
    y,
    width: right - x,
    height: bottom - y,
  };
}

function dedupeBoxes(boxes, imageWidth, imageHeight) {
  const deduped = [];
  for (const rawBox of boxes) {
    const box = clampBox(rawBox, imageWidth, imageHeight);
    if (box.width < 20 || box.height < 20) {
      continue;
    }
    if (deduped.some((existing) => boxOverlapRatio(existing, box) > 0.88)) {
      continue;
    }
    deduped.push(box);
  }
  return deduped;
}

function boxOverlapRatio(left, right) {
  const x1 = Math.max(left.x, right.x);
  const y1 = Math.max(left.y, right.y);
  const x2 = Math.min(left.x + left.width, right.x + right.width);
  const y2 = Math.min(left.y + left.height, right.y + right.height);
  if (x2 <= x1 || y2 <= y1) {
    return 0;
  }
  const intersection = (x2 - x1) * (y2 - y1);
  const smallerArea = Math.min(left.width * left.height, right.width * right.height);
  return intersection / smallerArea;
}

function decodeImage(buffer) {
  if (isPng(buffer)) {
    return decodePngImage(buffer);
  }
  if (isJpeg(buffer)) {
    return decodeJpegImage(buffer);
  }
  throw new Error("图片不是可识别的 JPG 或 PNG 格式");
}

function isPng(buffer) {
  return buffer.length >= PNG_SIGNATURE.length && buffer.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE);
}

function isJpeg(buffer) {
  return buffer.length >= 2 && buffer[0] === 0xff && buffer[1] === 0xd8;
}

function decodePngImage(buffer) {
  const { PNG } = loadDependency("pngjs");
  try {
    const image = PNG.sync.read(buffer);
    return {
      data: new Uint8ClampedArray(image.data.buffer, image.data.byteOffset, image.data.byteLength),
      width: image.width,
      height: image.height,
    };
  } catch (_) {
    throw new Error("图片不是可识别的 PNG 格式");
  }
}

function decodeJpegImage(buffer) {
  const jpeg = loadDependency("jpeg-js");
  try {
    const image = jpeg.decode(buffer, { useTArray: true });
    return {
      data: new Uint8ClampedArray(image.data.buffer, image.data.byteOffset, image.data.byteLength),
      width: image.width,
      height: image.height,
    };
  } catch (_) {
    throw new Error("图片不是可识别的 JPG 格式");
  }
}

function parseRawQuery(raw) {
  const query = raw.includes("?") ? raw.slice(raw.indexOf("?") + 1) : "";
  const params = {};
  for (const part of query.split("&")) {
    if (!part) continue;
    const equalsIndex = part.indexOf("=");
    const key = equalsIndex >= 0 ? part.slice(0, equalsIndex) : part;
    const value = equalsIndex >= 0 ? part.slice(equalsIndex + 1) : "";
    params[decodeURIComponent(key)] = decodeURIComponent(value).replace(/ /g, "+");
  }
  return params;
}

function classifyQr(raw, imageIndex) {
  if (/^https?:\/\//i.test(raw)) {
    return classifyActivityUrl(raw, imageIndex);
  }
  return classifyEncryptedScanQr(raw, imageIndex);
}

function classifyActivityUrl(raw, imageIndex) {
  const params = parseRawQuery(raw);
  const sourceNameParam = paramValue(params, "sourceName", "sourcename");
  const activityIdParam = paramValue(params, "activityid", "activityId", "activity_id");
  if (sourceNameParam && activityIdParam) {
    const sourceName = decryptOssText(sourceNameParam);
    const activityId = decryptOssText(activityIdParam);
    if (sourceName === ACTIVITY_QR_SOURCE && activityId) {
      return {
        kind: "activity_join",
        imageIndex,
        activityId,
      };
    }
  }
  const plainActivityId = paramValue(params, "activityid", "activityId", "activity_id", "id");
  if (/ekta\.qdgw\.edu\.cn/i.test(raw) && plainActivityId && /^\d+$/.test(plainActivityId)) {
    return {
      kind: "activity_join",
      imageIndex,
      activityId: plainActivityId,
    };
  }
  return {
    kind: "unknown",
    imageIndex,
    reason: "不是第二课堂活动二维码",
  };
}

function classifyEncryptedScanQr(raw, imageIndex) {
  const encrypted = raw.replace("?yiban=yiban_scan_result", "");
  let decrypted;
  try {
    decrypted = decryptAppText(encrypted);
  } catch (_) {
    return {
      kind: "unknown",
      imageIndex,
      reason: "不是第二课堂签到二维码",
    };
  }
  const query = decrypted.includes("?") ? decrypted.slice(decrypted.indexOf("?") + 1) : "";
  const params = Object.fromEntries(new URLSearchParams(query));
  const activityId = paramValue(params, "activityId", "activityid", "activity_id");
  const userId = paramValue(params, "userId", "userid", "user_id");
  if (!activityId || !userId) {
    return {
      kind: "unknown",
      imageIndex,
      reason: "不是第二课堂签到二维码",
    };
  }
  const requestType = getQrRequestType(decrypted);
  if (!requestType) {
    return {
      kind: "unknown",
      imageIndex,
      reason: "签到二维码类型未知",
    };
  }
  return {
    kind: "sign",
    imageIndex,
    activityId,
    scanUserId: userId,
    requestType,
    sp: params.sp || "",
  };
}

function paramValue(params, ...names) {
  for (const name of names) {
    if (params[name]) return params[name];
  }
  const normalized = {};
  for (const [key, value] of Object.entries(params)) {
    normalized[key.toLowerCase()] = value;
  }
  for (const name of names) {
    const value = normalized[name.toLowerCase()];
    if (value) return value;
  }
  return "";
}

function getQrRequestType(decrypted) {
  if (decrypted.includes("qutuo://sign")) return 2;
  if (decrypted.includes("qutuo://waitSign")) return 1;
  return "";
}

async function decodeTasks(imageSources) {
  const tasks = [];
  const skippedImages = [];
  for (let i = 0; i < imageSources.length; i += 1) {
    try {
      const raw = await decodeImageQr(imageSources[i]);
      const task = classifyQr(raw, i + 1);
      if (task.kind === "unknown") {
        skippedImages.push({
          imageIndex: i + 1,
          reason: task.reason || "二维码类型未知",
        });
      } else {
        tasks.push(task);
      }
    } catch (error) {
      skippedImages.push({
        imageIndex: i + 1,
        reason: error.message || String(error),
      });
    }
  }
  tasks.sort((left, right) => taskRank(left) - taskRank(right) || left.imageIndex - right.imageIndex);
  return { tasks, skippedImages };
}

function taskRank(task) {
  if (task.kind === "activity_join") return 0;
  if (task.kind === "sign") return 1;
  return 2;
}

async function getMemberIdentity(activityId, token) {
  const identity = await apiGet("activity/member/identity", { activityId }, token);
  if (identity.body.code !== 0) {
    return {
      ok: false,
      codeStatus: identity.body.code,
      message: identity.body.msg || "读取活动身份失败",
      identity: "",
    };
  }
  return {
    ok: true,
    codeStatus: 0,
    message: "",
    identity: Number(identity.body.data && identity.body.data.identity),
  };
}

async function getNonMemberActivity(activityId, token) {
  const detail = await apiGet("activity/detail/non-member", { id: activityId }, token);
  if (detail.body.code !== 0) {
    return {
      ok: false,
      codeStatus: detail.body.code,
      message: detail.body.msg || "读取活动失败",
      activity: {},
    };
  }
  return {
    ok: true,
    codeStatus: 0,
    message: "",
    activity: detail.body.data || {},
  };
}

async function handleActivityJoin(account, fallbackSchoolCode, task, dryRun) {
  try {
    const session = await loginAccount(account, fallbackSchoolCode);
    if (session.error) return session.error;

    const before = await getMemberIdentity(task.activityId, session.token);
    if (before.ok && [1, 2, 3].includes(before.identity)) {
      return {
        ok: true,
        codeStatus: before.identity === 1 ? "ALREADY_JOINED" : "ALREADY_HAS_ROLE",
        message: before.identity === 1 ? "已加入活动" : "已有活动身份",
      };
    }

    const detail = await getNonMemberActivity(task.activityId, session.token);
    if (!detail.ok) {
      return {
        ok: false,
        codeStatus: detail.codeStatus,
        message: detail.message,
      };
    }

    if (Number(detail.activity.enrollMaterial) === 1) {
      return {
        ok: false,
        codeStatus: "REQUIRES_MATERIAL",
        message: "活动需要报名材料，已跳过",
      };
    }

    if (dryRun) {
      return {
        ok: true,
        codeStatus: "DRY_RUN",
        message: "未提交，预计报名活动",
      };
    }

    const enroll = await apiPost(
      "activity/enroll/person",
      { id: task.activityId, personMaterial: [] },
      session.token,
    );
    if (enroll.body.code !== 0) {
      return {
        ok: false,
        codeStatus: enroll.body.code,
        message: enroll.body.msg || "报名失败",
      };
    }

    const after = await getMemberIdentity(task.activityId, session.token);
    if (after.ok && [1, 2, 3].includes(after.identity)) {
      return {
        ok: true,
        codeStatus: 0,
        message: "加入活动成功",
      };
    }
    return {
      ok: true,
      codeStatus: "PENDING_REVIEW",
      message: "报名已提交，等待审核或身份刷新",
    };
  } catch (error) {
    return {
      ok: false,
      codeStatus: "REQUEST_ERROR",
      message: error.message || String(error),
    };
  }
}

function getOpenAction(activity) {
  if (Number(activity.signSwitch) === 1) {
    return { name: "签到", historyType: 1, alreadyStatus: "ALREADY_SIGNED_IN", alreadyMessage: "已签到" };
  }
  if (Number(activity.signSwitch) === 2) {
    return { name: "签退", historyType: 2, alreadyStatus: "ALREADY_SIGNED_OUT", alreadyMessage: "已签退" };
  }
  return { name: "未开启", historyType: 0, alreadyStatus: "SIGN_CLOSED", alreadyMessage: "" };
}

function hasHistoryType(history, type) {
  return Array.isArray(history) && history.some((item) => Number(item.type) === Number(type));
}

async function handleSign(account, fallbackSchoolCode, task, dryRun) {
  try {
    const session = await loginAccount(account, fallbackSchoolCode);
    if (session.error) return session.error;

    const detail = await apiGet("activity/detail/participant", { id: task.activityId }, session.token);
    if (detail.body.code !== 0) {
      return {
        ok: false,
        codeStatus: detail.body.code,
        message: detail.body.msg || "读取活动失败",
      };
    }

    const activity = detail.body.data || {};
    const action = getOpenAction(activity);
    const history = await apiPost(
      "activity/sign/one/list",
      { userId: session.user.id, activityId: task.activityId },
      session.token,
    );
    const records = history.body.code === 0 ? history.body.data : [];

    if (action.historyType === 0) {
      return {
        ok: false,
        codeStatus: action.alreadyStatus,
        message: "当前活动未开启签到/签退",
      };
    }

    if (hasHistoryType(records, action.historyType)) {
      return {
        ok: true,
        codeStatus: action.alreadyStatus,
        message: action.alreadyMessage,
      };
    }

    if (dryRun) {
      return {
        ok: true,
        codeStatus: "DRY_RUN",
        message: `未提交，预计执行${action.name}`,
      };
    }

    const scan = await apiPost(
      "activity/sign/in-out",
      {
        id: task.activityId,
        userId: task.scanUserId,
        type: task.requestType,
        sp: task.sp || "",
      },
      session.token,
    );
    return {
      ok: scan.body.code === 0,
      codeStatus: scan.body.code,
      message: scan.body.code === 0 ? `${action.name}成功` : scan.body.msg || `${action.name}失败`,
    };
  } catch (error) {
    return {
      ok: false,
      codeStatus: "REQUEST_ERROR",
      message: error.message || String(error),
    };
  }
}

function emptyActionCounts() {
  return { total: 0, ok: 0, already: 0, failed: 0, skipped: 0 };
}

function countResult(counts, result) {
  counts.total += 1;
  const status = String(result.codeStatus || "");
  if (status === "DRY_RUN" || status === "REQUIRES_MATERIAL") {
    counts.skipped += 1;
  } else if (status.startsWith("ALREADY_")) {
    counts.already += 1;
  } else if (result.ok) {
    counts.ok += 1;
  } else {
    counts.failed += 1;
  }
}

function accountCode(account) {
  return account.code || account.username || account.account || "";
}

function statusFromResult(result) {
  const codeStatus = String(result.codeStatus || "");
  if (codeStatus === "DRY_RUN") return "未提交";
  if (codeStatus === "REQUIRES_MATERIAL") return "跳过";
  if (codeStatus.startsWith("ALREADY_")) return "已完成";
  if (result.ok) return "成功";
  return "失败";
}

function taskLabel(task) {
  return task.kind === "activity_join" ? "活动加入" : "签到处理";
}

function writeJsonLine(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function decodeOnlyPayload(options, tasks, skippedImages) {
  return {
    ok: true,
    dryRun: true,
    summary: {
      imageCount: options.images.length,
      skippedImageCount: skippedImages.length,
      recognizedCount: tasks.length,
      activityTaskCount: tasks.filter((task) => task.kind === "activity_join").length,
      signTaskCount: tasks.filter((task) => task.kind === "sign").length,
      accountCount: 0,
      actionCounts: {
        activity_join: emptyActionCounts(),
        sign: emptyActionCounts(),
      },
    },
    tasks,
    skippedImages,
    taskSummaries: tasks.map((task) => ({
      kind: task.kind,
      imageIndex: task.imageIndex,
      activityId: task.activityId,
    })),
  };
}

async function executeTask(options, task) {
  const accounts = parseCsv(fs.readFileSync(options.accountsPath, "utf8"));
  if (accounts.length === 0) {
    throw new Error(`CSV 没有账号行: ${options.accountsPath}`);
  }
  if (accounts.length > options.maxAccounts) {
    throw new Error(`账号数超过上限: ${accounts.length}/${options.maxAccounts}`);
  }

  const defaultSchool = options.schoolCode
    ? { schoolCode: String(options.schoolCode), schoolName: "" }
    : await getDefaultSchoolCode();
  const counts = emptyActionCounts();
  const results = [];

  if (options.jsonl) {
    writeJsonLine({
      type: "task_start",
      kind: task.kind,
      label: taskLabel(task),
      activityId: task.activityId,
      accountCount: accounts.length,
      dryRun: options.dryRun,
    });
  }

  for (let i = 0; i < accounts.length; i += 1) {
    const result = task.kind === "activity_join"
      ? await handleActivityJoin(accounts[i], defaultSchool.schoolCode, task, options.dryRun)
      : await handleSign(accounts[i], defaultSchool.schoolCode, task, options.dryRun);
    const record = {
      accountCode: accountCode(accounts[i]),
      ok: Boolean(result.ok),
      codeStatus: String(result.codeStatus || ""),
      status: statusFromResult(result),
      message: result.message || "",
    };
    countResult(counts, result);
    results.push(record);
    if (options.jsonl) {
      writeJsonLine({
        type: "account_result",
        kind: task.kind,
        activityId: task.activityId,
        index: i + 1,
        accountCount: accounts.length,
        ...record,
      });
    }
    if (i < accounts.length - 1 && options.delayMs > 0) {
      await sleep(options.delayMs);
    }
  }

  const payload = {
    type: "task_done",
    kind: task.kind,
    label: taskLabel(task),
    activityId: task.activityId,
    dryRun: options.dryRun,
    counts,
    results,
  };
  if (options.jsonl) {
    writeJsonLine(payload);
  }
  return payload;
}

async function execute(options) {
  const { tasks, skippedImages } = await decodeTasks(options.images);
  if (options.decodeOnly) {
    return decodeOnlyPayload(options, tasks, skippedImages);
  }

  const accounts = parseCsv(fs.readFileSync(options.accountsPath, "utf8"));
  if (accounts.length === 0) {
    throw new Error(`CSV 没有账号行: ${options.accountsPath}`);
  }
  if (accounts.length > options.maxAccounts) {
    throw new Error(`账号数超过上限: ${accounts.length}/${options.maxAccounts}`);
  }
  if (tasks.length === 0) {
    return {
      ok: true,
      dryRun: options.dryRun,
      summary: {
        imageCount: options.images.length,
        skippedImageCount: skippedImages.length,
        recognizedCount: 0,
        activityTaskCount: 0,
        signTaskCount: 0,
        accountCount: accounts.length,
        actionCounts: {
          activity_join: emptyActionCounts(),
          sign: emptyActionCounts(),
        },
      },
      taskSummaries: [],
    };
  }

  const defaultSchool = options.schoolCode
    ? { schoolCode: String(options.schoolCode), schoolName: "" }
    : await getDefaultSchoolCode();
  const actionCounts = {
    activity_join: emptyActionCounts(),
    sign: emptyActionCounts(),
  };
  const taskSummaries = [];

  for (const task of tasks) {
    const counts = emptyActionCounts();
    const results = [];
    for (let i = 0; i < accounts.length; i += 1) {
      const result = task.kind === "activity_join"
        ? await handleActivityJoin(accounts[i], defaultSchool.schoolCode, task, options.dryRun)
        : await handleSign(accounts[i], defaultSchool.schoolCode, task, options.dryRun);
      countResult(counts, result);
      countResult(actionCounts[task.kind], result);
      results.push({
        accountCode: accountCode(accounts[i]),
        ok: Boolean(result.ok),
        codeStatus: String(result.codeStatus || ""),
        message: result.message || "",
      });
      if (i < accounts.length - 1 && options.delayMs > 0) {
        await sleep(options.delayMs);
      }
    }
    taskSummaries.push({
      kind: task.kind,
      imageIndex: task.imageIndex,
      activityId: task.activityId,
      counts,
      results,
    });
  }

  return {
    ok: true,
    dryRun: options.dryRun,
    summary: {
      imageCount: options.images.length,
      skippedImageCount: skippedImages.length,
      recognizedCount: tasks.length,
      activityTaskCount: tasks.filter((task) => task.kind === "activity_join").length,
      signTaskCount: tasks.filter((task) => task.kind === "sign").length,
      accountCount: accounts.length,
      actionCounts,
    },
    taskSummaries,
  };
}

async function main() {
  const options = readArgs(process.argv.slice(2));
  if (options.taskJson) {
    const task = JSON.parse(options.taskJson);
    const payload = await executeTask(options, task);
    if (!options.jsonl) {
      process.stdout.write(JSON.stringify(payload));
    }
    return;
  }

  const payload = await execute(options);
  process.stdout.write(JSON.stringify(payload));
}

main().catch((error) => {
  process.stderr.write(error.message || String(error));
  process.exit(1);
});
