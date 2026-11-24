(function () {
    var cfg = /*__CAPTURE_CONFIG__*/ null;
    if (!cfg) { alert("请运行导出文件夹中的「导入蓝扇序列.jsx」。"); return; }
    var root = File($.fileName).parent;
    var project = app.project || app.newProject();
    var seqFps = 30, screenFps = 60;
    var count = cfg.times.length;
    var group, importedItems = [];
    app.beginUndoGroup("导入实机蓝扇序列");
    try {
        if (project.numItems === 0 && !project.file) {
            project.bitsPerChannel = 32;
            project.workingSpace = "sRGB IEC61966-2.1";
            project.linearizeWorkingSpace = true;
        }
        if (project.bitsPerChannel !== 32 || !project.linearizeWorkingSpace || project.workingSpace !== "sRGB IEC61966-2.1") {
            throw Error("请在 32 bpc、已线性化的 sRGB IEC61966-2.1 工程内运行，或新建一个空工程再运行。");
        }
        group = project.items.addFolder("蓝扇 · 游戏连续帧");
        function footage(key) {
            var file = File(root.fsName + "/" + cfg.paths[key]);
            if (!file.exists) throw Error("找不到素材 " + cfg.paths[key]);
            var opts = new ImportOptions(file);
            opts.sequence = (key !== "plate");
            var item = project.importFile(opts);
            importedItems.push(item);
            item.parentFolder = group;
            if (item.width !== cfg.width || item.height !== cfg.height || item.footageMissing) {
                throw Error("素材尺寸或路径异常 " + file.name);
            }
            if (opts.sequence) {
                item.mainSource.conformFrameRate = seqFps;
                if (Math.round(item.duration * seqFps) !== count) throw Error("序列帧数不符 " + file.name);
            }
            return item;
        }
        var plate = footage("plate"), black = footage("black"), mult = footage("multiply");
        var add = footage("add"), game = footage("game");
        function comp(name, duration, fps) {
            var c = project.items.addComp(name, cfg.width, cfg.height, 1, duration, fps);
            importedItems.push(c);
            c.parentFolder = group;
            return c;
        }
        // Each source frame has its own index in the editable comp, including blank presents.
        var edit = comp("02 逐帧核对与换底", count / seqFps, seqFps);
        edit.layers.add(plate).name = "底图（可替换）";
        edit.layers.add(black).name = "暗底 · 游戏逐帧遮挡";
        var m = edit.layers.add(mult); m.name = "颜色响应"; m.blendingMode = BlendingMode.MULTIPLY;
        var a = edit.layers.add(add); a.name = "扇面颜色与电纹"; a.blendingMode = BlendingMode.ADD;
        var ref = edit.layers.add(game);
        ref.name = "游戏同帧原画（开关眼睛对照）";
        ref.enabled = false;
        // The 60-fps viewer samples a hold at each actual presentation timestamp.
        var realtime = comp("01 实速预览", Math.ceil(cfg.duration * screenFps) / screenFps, screenFps);
        var layer = realtime.layers.add(edit);
        layer.name = "双击查看分层与游戏原画";
        layer.timeRemapEnabled = true;
        layer.outPoint = realtime.duration;
        var remap = layer.property("ADBE Time Remapping");
        // Keep the first key until replacement so AE retains the remap property.
        while (remap.numKeys > 1) remap.removeKey(remap.numKeys);
        var values = [];
        for (var i = 0; i < count; i++) values.push((i + 0.5) / seqFps);
        remap.setValuesAtTimes(cfg.times, values);
        for (var j = 1; j <= remap.numKeys; j++) {
            remap.setInterpolationTypeAtKey(j, KeyframeInterpolationType.HOLD, KeyframeInterpolationType.HOLD);
        }
        realtime.openInViewer();
    } catch (error) {
        for (var index = importedItems.length - 1; index >= 0; index--) {
            try { importedItems[index].remove(); } catch (ignored) {}
        }
        if (group) { try { group.remove(); } catch (ignoredGroup) {} }
        alert("蓝扇导入未完成。" + "\n" + error.toString());
    } finally {
        app.endUndoGroup();
    }
})();
