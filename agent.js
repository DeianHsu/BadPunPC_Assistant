/* Loaded after frida-il2cpp-bridge. Unity work runs after its synchronization queue. */
let bridgeReady = false;
let bridgeError = null;
let gameRoot = null;
let gameImage = null;
let coreImage = null;
let mainHook = null;
let bootstrapHook = null;
let gameWindow = null;
let originalBackground = null;
let lastTick = 0;
const pending = [];

function enableBackground() {
    if (originalBackground !== null) return;
    const application = coreImage.class("UnityEngine.Application");
    originalBackground = application.method("get_runInBackground", 0).invoke();
    application.method("set_runInBackground", 1).invoke(true);
}

function bootstrapBackground() {
    // The Windows message pump stays alive even when Unity pauses due to lost focus.
    // Wake it with an unused WM_APP message; execute only on Unity's verified main thread.
    const user32 = Process.getModuleByName("user32.dll");
    const threadOfWindow = new NativeFunction(user32.getExportByName("GetWindowThreadProcessId"), "uint", ["pointer", "pointer"]);
    const visible = new NativeFunction(user32.getExportByName("IsWindowVisible"), "int", ["pointer"]);
    const enumWindows = new NativeFunction(user32.getExportByName("EnumWindows"), "int", ["pointer", "pointer"]);
    const postMessage = new NativeFunction(user32.getExportByName("PostMessageW"), "int", ["pointer", "uint", "pointer", "pointer"]);
    const mainId = Il2Cpp.mainThread.id;
    let window = null;
    const processId = Memory.alloc(4);
    const enumerate = new NativeCallback(hwnd => {
        const threadId = threadOfWindow(hwnd, processId);
        if (processId.readU32() === Process.id && threadId === mainId && visible(hwnd)) {
            window = hwnd;
            return 0;
        }
        return 1;
    }, "int", ["pointer", "pointer"]);
    enumWindows(enumerate, NULL);
    if (window === null) return; // Foreground initialization remains available as a fallback.
    gameWindow = window;
    bootstrapHook = Interceptor.attach(user32.getExportByName("DispatchMessageW"), {
        onLeave() {
            if (Process.getCurrentThreadId() !== mainId || originalBackground !== null) return;
            try {
                enableBackground();
                bootstrapHook.detach();
                send({ type: "background_enabled" });
            } catch (error) {
                bridgeError = String(error);
            }
        }
    });
    postMessage(window, 0x8331, NULL, NULL);
}

function textOf(value) {
    if (value == null || value.isNull?.()) return "";
    const textGetter = value.tryMethod?.("get_text", 0);
    if (textGetter) return textOf(textGetter.invoke());
    return value.content ?? String(value);
}

function numberOf(value) {
    if (typeof value === "number") return value;
    if (typeof value === "boolean") return Number(value);
    if (value?.field) return Number(value.field("value__").value);
    return Number(value);
}

function mainThread(action) {
    if (bridgeError) return Promise.reject(new Error(bridgeError));
    if (!bridgeReady) return Promise.reject(new Error("游戏接口仍在初始化"));
    return new Promise((resolve, reject) => {
        const item = { action, resolve, reject };
        item.timer = setTimeout(() => {
            const index = pending.indexOf(item);
            if (index >= 0) pending.splice(index, 1);
            reject(new Error("等待游戏主线程超时；请恢复游戏窗口后重试"));
        }, 12000);
        pending.push(item);
    });
}

function findActive(name) {
    const klass = gameImage.class(name);
    const resources = coreImage.class("UnityEngine.Resources");
    const all = resources.method("FindObjectsOfTypeAll", 1).overload("System.Type").invoke(klass.type.object);
    for (const obj of all) {
        if (!obj.isNull() && obj.method("get_isActiveAndEnabled", 0).invoke()) return obj;
    }
    throw new Error(`没有活动的 ${name}，请进入单人题目`);
}

function currentContext() {
    if (!gameRoot || gameRoot.isNull()) throw new Error("游戏尚未进入主循环");
    if (!gameRoot.method("get_IsInLocalGame", 0).invoke()) {
        throw new Error("当前不是单人模式，请先进入单人题目");
    }
    const manager = gameRoot.method("get_CurrentGameManager", 0).invoke();
    if (manager.isNull()) throw new Error("题目正在加载");
    const puzzle = findActive("GamePuzzleArea");
    const level = puzzle.method("get_currentLevelData", 0).invoke();
    if (level.isNull()) throw new Error("题目数据尚未加载");
    return { manager, puzzle, level };
}

function stateName(manager) {
    const code = numberOf(manager.method("get_GameState", 0).invoke());
    return enumName(code, "GameState");
}

function enumName(value, className) {
    const code = numberOf(value);
    const type = gameImage.class(className);
    for (const field of type.fields) {
        if (field.isStatic && numberOf(field.value) === code) return field.name;
    }
    return String(code);
}

function feedbackHistory() {
    const rowType = gameImage.class("GuessHistoryItem");
    const boxType = gameImage.class("CharacterBox");
    const rows = coreImage.class("UnityEngine.Resources").method("FindObjectsOfTypeAll", 1).overload("System.Type").invoke(rowType.type.object);
    const output = [];
    const feedbackNames = { Wrong: "absent", Exist: "present_elsewhere", Correct: "correct_position" };
    for (const row of rows) {
        if (row.isNull() || !row.method("get_isActiveAndEnabled", 0).invoke()) continue;
        const container = row.field("characterBoxContainer").value;
        const letters = [];
        const count = container.method("get_childCount", 0).invoke();
        for (let i = 0; i < count; i++) {
            const child = container.method("GetChild", 1).invoke(i);
            const box = child.method("GetComponent", 1).overload("System.Type").invoke(boxType.type.object);
            if (box.isNull()) continue;
            const hit = box.field("hitResult").value;
            if (hit.isNull?.()) continue;
            const character = hit.field("Character").value;
            const pinyin = hit.field("Pinyin").value;
            letters.push({
                position: letters.length + 1,
                character: typeof character === "number" ? String.fromCharCode(character) : textOf(character),
                pinyin: pinyin.isNull?.() ? "" : textOf(pinyin.method("ToRawString", 0).invoke()),
                character_feedback: feedbackNames[enumName(hit.field("CharHitResult").value, "GuessResult")],
                pinyin_feedback: feedbackNames[enumName(hit.field("PinyinHitResult").value, "GuessResult")],
                tone_feedback: feedbackNames[enumName(hit.method("get_ToneHitResult", 0).invoke(), "GuessResult")]
            });
        }
        if (letters.length) output.push({
            order: row.method("get_transform", 0).invoke().method("GetSiblingIndex", 0).invoke(),
            guess: letters.map(letter => letter.character).join(""), letters
        });
    }
    output.sort((a, b) => a.order - b.order);
    return output;
}

function levelInfo(context) {
    const { manager, level } = context;
    // Only public puzzle information is exposed. Never access the stored solution.
    return {
        level_id: textOf(level.method("get_Id", 0).invoke()),
        display_index: numberOf(level.method("get_DisplayIndex", 0).invoke()),
        reference_word: textOf(level.method("get_RefWordText", 0).invoke()),
        puzzle_text: textOf(level.method("get_PuzzleWordText", 0).invoke()),
        category: textOf(context.puzzle.method("get_levelTypeText", 0).invoke()),
        expected_length: numberOf(manager.method("get_ExpectedGuessLength", 0).invoke()),
        is_english: Boolean(level.method("get_IsEnglish", 0).invoke()),
        is_video: Boolean(level.method("get_IsVideo", 0).invoke()),
        state: stateName(manager),
        retry_count: numberOf(manager.field("retryCount").value),
        feedback_history: feedbackHistory()
    };
}

function base64(bytes) {
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    const data = new Uint8Array(bytes);
    const chunks = [];
    let output = "";
    for (let i = 0; i < data.length; i += 3) {
        const a = data[i], b = data[i + 1] ?? 0, c = data[i + 2] ?? 0;
        output += alphabet[a >> 2] + alphabet[((a & 3) << 4) | (b >> 4)]
            + (i + 1 < data.length ? alphabet[((b & 15) << 2) | (c >> 6)] : "=")
            + (i + 2 < data.length ? alphabet[c & 63] : "=");
        if (output.length >= 16384) { chunks.push(output); output = ""; }
    }
    chunks.push(output);
    return chunks.join("");
}

function exportSprite(imageComponent) {
    const sprite = imageComponent.method("get_sprite", 0).invoke();
    if (sprite.isNull()) throw new Error("题面图片尚未加载");
    const texture = sprite.method("get_texture", 0).invoke();
    const width = texture.method("get_width", 0).invoke();
    const height = texture.method("get_height", 0).invoke();
    const rect = sprite.method("get_rect", 0).invoke();
    const crop = ["get_x", "get_y", "get_width", "get_height"].map(name => rect.method(name, 0).invoke());
    const conversion = Il2Cpp.domain.assembly("UnityEngine.ImageConversionModule").image.class("UnityEngine.ImageConversion");
    const objectType = coreImage.class("UnityEngine.Object");
    const renderType = coreImage.class("UnityEngine.RenderTexture");
    let cpuTexture = null;
    let temporary = null;
    let previous = null;
    let changedTarget = false;
    try {
        let source = texture;
        if (!texture.method("get_isReadable", 0).invoke()) {
            temporary = renderType.method("GetTemporary", 3).overload("System.Int32", "System.Int32", "System.Int32").invoke(width, height, 0);
            coreImage.class("UnityEngine.Graphics").method("Blit", 2).overload("UnityEngine.Texture", "UnityEngine.RenderTexture").invoke(texture, temporary);
            previous = renderType.method("get_active", 0).invoke();
            renderType.method("set_active", 1).invoke(temporary);
            changedTarget = true;
            const cpuType = coreImage.class("UnityEngine.Texture2D");
            cpuTexture = cpuType.alloc();
            cpuTexture.method(".ctor", 4).overload("System.Int32", "System.Int32", "UnityEngine.TextureFormat", "System.Boolean").invoke(width, height, 4, false);
            const rectType = coreImage.class("UnityEngine.Rect");
            const fullRect = new Il2Cpp.ValueType(Memory.alloc(rectType.valueTypeSize), rectType.type);
            fullRect.method(".ctor", 4).invoke(0, 0, width, height);
            cpuTexture.method("ReadPixels", 4).invoke(fullRect, 0, 0, false);
            cpuTexture.method("Apply", 2).invoke(false, false);
            source = cpuTexture;
        }
        const png = conversion.method("EncodeToPNG", 1).invoke(source);
        if (png.isNull() || png.length === 0) throw new Error("无法导出题面纹理");
        return { png_base64: base64(png.elements.handle.readByteArray(png.length)), width, height, crop };
    } finally {
        if (changedTarget) renderType.method("set_active", 1).invoke(previous);
        if (temporary) renderType.method("ReleaseTemporary", 1).invoke(temporary);
        if (cpuTexture) objectType.method("Destroy", 1).overload("UnityEngine.Object").invoke(cpuTexture);
    }
}

rpc.exports = {
    pronunciations(words) {
        return mainThread(() => {
            const tagger = findActive("PinyinTagger");
            // C# char is UTF-16; the bridge's generic invoker uses an 8-bit char alias.
            const method = tagger.method("GetPinyin", 1);
            const getPinyin = new NativeFunction(method.virtualAddress, "pointer", ["pointer", "uint16", "pointer"]);
            const output = {};
            for (const word of words) {
                output[word] = [...word].map(char => {
                    const readings = new Il2Cpp.Array(getPinyin(tagger.handle, char.charCodeAt(0), method.handle));
                    return [...readings].map(pinyin => textOf(pinyin.method("ToRawString", 0).invoke()));
                });
            }
            return output;
        });
    },
    restart(levelId) {
        return mainThread(() => {
            const context = currentContext();
            const info = levelInfo(context);
            if (info.level_id !== levelId) throw new Error("题目已变化，未重开旧题");
            if (info.state !== "Failed") throw new Error("仅在试错耗尽后自动重开本关");
            context.manager.method("ReloadLevel", 0).invoke();
            return { requested: true, level_id: levelId };
        });
    },
    health() {
        const status = { ready: bridgeReady, error: bridgeError, pid: Process.id, last_tick_age_ms: lastTick ? Date.now() - lastTick : null };
        if (gameWindow !== null) {
            const user32 = Process.getModuleByName("user32.dll");
            const minimized = new NativeFunction(user32.getExportByName("IsIconic"), "int", ["pointer"]);
            const foreground = new NativeFunction(user32.getExportByName("GetForegroundWindow"), "pointer", []);
            status.window_minimized = Boolean(minimized(gameWindow));
            status.window_foreground = foreground().equals(gameWindow);
        }
        return status;
    },
    status() { return mainThread(() => levelInfo(currentContext())); },
    puzzle() {
        return mainThread(() => {
            const context = currentContext();
            const info = levelInfo(context);
            if (info.is_video || info.is_english) throw new Error("当前版本仅支持中文静态图片题，视频题／英文题请手动处理");
            return { ...info, images: [
                exportSprite(context.puzzle.field("refImage").value),
                exportSprite(context.puzzle.field("puzzleImage").value)
            ] };
        });
    },
    submit(levelId, answer) {
        return mainThread(() => {
            const context = currentContext();
            const info = levelInfo(context);
            if (info.level_id !== levelId) throw new Error("题目已经变化，未提交旧答案");
            if (info.state !== "Playing") throw new Error(`当前状态 ${info.state} 无法提交`);
            if (typeof answer !== "string" || [...answer].length !== info.expected_length) throw new Error("答案字数不符合题目要求");
            const input = findActive("GameInputArea");
            input.method("SetInput", 1).invoke(Il2Cpp.string(answer));
            input.method("OnSubmitButtonClicked", 0).invoke();
            return levelInfo(context);
        });
    },
    next(levelId) {
        return mainThread(() => {
            const context = currentContext();
            const info = levelInfo(context);
            if (info.level_id !== levelId) throw new Error("题目已经变化，未重复跳题");
            if (info.state !== "Win") throw new Error("只有答对后才会进入下一题");
            // Both Chinese and English result areas can have enabled components.
            // Use the game's selected area instead of taking the first scene object.
            const result = findActive("LocalGameUIManager").method("get_CurrentResultArea", 0).invoke();
            for (const name of ["get_NextLevelButton", "get_WinButton"]) {
                const button = result.method(name, 0).invoke();
                if (button.method("get_isActiveAndEnabled", 0).invoke() && button.method("get_interactable", 0).invoke()) {
                    button.method("get_onClick", 0).invoke().method("Invoke", 0).invoke();
                    return { requested: true, previous_level_id: levelId };
                }
            }
            return { requested: false, reason: "button_not_ready" };
        });
    },
    cleanup() {
        if (originalBackground === null) return { restored: true };
        return mainThread(() => {
            if (originalBackground !== null) coreImage.class("UnityEngine.Application").method("set_runInBackground", 1).invoke(originalBackground);
            return { restored: true };
        });
    }
};

Il2Cpp.perform(() => {
    gameImage = Il2Cpp.domain.assembly("Assembly-CSharp").image;
    coreImage = Il2Cpp.domain.assembly("UnityEngine.CoreModule").image;
    // GameManager.Update can compile to a tiny return stub that Frida cannot intercept.
    // UnitySynchronizationContext.Exec is the engine's established main-thread queue.
    const update = coreImage.class("UnityEngine.UnitySynchronizationContext").method("Exec", 0);
    mainHook = Interceptor.attach(update.virtualAddress, {
        onLeave() {
            lastTick = Date.now();
            if (gameRoot === null) {
                try { gameRoot = findActive("GameManager"); }
                catch (_) { /* The scene may still be loading. */ }
            }
            if (originalBackground === null) {
                try {
                    enableBackground();
                } catch (error) { bridgeError = String(error); }
            }
            const item = pending.shift();
            if (item) {
                clearTimeout(item.timer);
                try { item.resolve(item.action()); }
                catch (error) { item.reject(new Error(String(error))); }
            }
        }
    });
    bridgeReady = true;
    bootstrapBackground();
    send({ type: "ready", unity: Il2Cpp.unityVersion });
}).catch(error => {
    bridgeError = String(error);
    send({ type: "bridge_error", error: bridgeError });
});
