/* Calibre-Web Automated – OneDrive import page (templates/onedrive.html) */
/* global $ */
(function () {
    "use strict";

    var $root = $("#onedrive-import");
    if (!$root.length || $root.data("connected") !== true) {
        return;
    }

    var S = JSON.parse($("#od-strings").text());
    var urls = {
        children: $root.data("children-url"),
        importJobs: $root.data("import-url"),
        jobs: $root.data("jobs-url"),
        cancel: $root.data("cancel-url"),
        connect: $root.data("connect-url"),
        tasks: $root.data("tasks-url")
    };

    var path = [{id: null, name: S.root}];
    var items = [];
    var cursor = null;
    var selection = {};
    var listRequest = null;
    var jobsTimer = null;

    function fmt(template, values) {
        return template.replace(/\{(\w+)\}/g, function (m, key) {
            return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : m;
        });
    }

    function humanSize(bytes) {
        if (!bytes) { return "0 B"; }
        var units = ["B", "KB", "MB", "GB", "TB"];
        var i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
        return (bytes / Math.pow(1024, i)).toFixed(i ? 1 : 0) + " " + units[i];
    }

    function showError(message, reconnect) {
        var $err = $("#od-error").empty().removeClass("hidden").text(message + " ");
        if (reconnect) {
            $("<a>").attr("href", urls.connect).addClass("alert-link").text(S.reconnect).appendTo($err);
        }
    }

    function handleFailure(xhr) {
        var body = xhr.responseJSON || {};
        showError(body.error || S.error, !!body.reconnect);
    }

    // ── browsing ─────────────────────────────────────────────────────────

    function current() {
        return path[path.length - 1];
    }

    function renderBreadcrumb() {
        var $bc = $("#od-breadcrumb").empty();
        path.forEach(function (entry, index) {
            var $li = $("<li>");
            if (index === path.length - 1) {
                $li.addClass("active").text(entry.name);
            } else {
                $("<a href='#'>").text(entry.name).on("click", function (e) {
                    e.preventDefault();
                    path = path.slice(0, index + 1);
                    loadFolder();
                }).appendTo($li);
            }
            $bc.append($li);
        });
    }

    function renderList() {
        var filter = $("#od-filter").val().trim().toLowerCase();
        var $list = $("#od-list").empty();
        var visible = items.filter(function (item) {
            return !filter || item.name.toLowerCase().indexOf(filter) !== -1;
        });
        if (!visible.length && !listRequest) {
            $("<li class='list-group-item text-muted'>").text(items.length ? S.noMatch : S.empty).appendTo($list);
        }
        visible.forEach(function (item) {
            var $li = $("<li class='list-group-item od-item'>").toggleClass("od-disabled", !item.importable);
            var $label = $("<label class='od-check'>");
            var $cb = $("<input type='checkbox'>")
                .prop("checked", !!selection[item.id])
                .prop("disabled", !item.importable)
                .attr("aria-label", item.name)
                .on("change", function () {
                    toggle(item, this.checked);
                });
            $label.append($cb).appendTo($li);
            $("<span class='glyphicon od-icon' aria-hidden='true'>")
                .addClass(item.folder ? "glyphicon-folder-close" : "glyphicon-book")
                .appendTo($li);
            var $name = item.folder
                ? $("<a href='#' class='od-name'>").on("click", function (e) {
                    e.preventDefault();
                    path.push({id: item.id, name: item.name});
                    loadFolder();
                })
                : $("<span class='od-name'>");
            $name.text(item.name).appendTo($li);
            var meta = item.folder
                ? (item.childCount === 1 ? S.item : fmt(S.items, {n: item.childCount}))
                : humanSize(item.size);
            $("<span class='od-meta text-muted'>").text(meta).appendTo($li);
            if (!item.importable) {
                $li.attr("title", item.folder ? S.folderNotAllowed : S.unsupported);
            }
            $list.append($li);
        });
        var importable = visible.filter(function (i) { return i.importable; });
        var allSelected = importable.length > 0 && importable.every(function (i) { return selection[i.id]; });
        $("#od-select-all").prop("checked", allSelected).prop("disabled", !importable.length);
        $("#od-more").toggleClass("hidden", !cursor);
    }

    function sortItems() {
        items.sort(function (a, b) {
            if (a.folder !== b.folder) { return a.folder ? -1 : 1; }
            return a.name.localeCompare(b.name, undefined, {sensitivity: "base", numeric: true});
        });
    }

    function fetchPage(params, append) {
        if (listRequest) { listRequest.abort(); }
        $("#od-error").addClass("hidden");
        $("#od-loading").removeClass("hidden");
        $("#od-more").prop("disabled", true);
        listRequest = $.ajax({url: urls.children, data: params, dataType: "json"})
            .done(function (data) {
                items = append ? items.concat(data.items) : data.items;
                cursor = data.next || null;
                sortItems();
            })
            .fail(function (xhr, status) {
                if (status !== "abort") { handleFailure(xhr); }
            })
            .always(function () {
                listRequest = null;
                $("#od-loading").addClass("hidden");
                $("#od-more").prop("disabled", false);
                renderList();
            });
    }

    function loadFolder() {
        items = [];
        cursor = null;
        $("#od-filter").val("");
        renderBreadcrumb();
        renderList();
        var folder = current();
        fetchPage(folder.id ? {item: folder.id} : {}, false);
    }

    // ── selection ────────────────────────────────────────────────────────

    function toggle(item, checked) {
        if (checked) {
            selection[item.id] = {id: item.id, name: item.name, folder: item.folder};
        } else {
            delete selection[item.id];
        }
        renderSelection();
        renderList();
    }

    function renderSelection() {
        var files = 0, folders = 0;
        Object.keys(selection).forEach(function (id) {
            if (selection[id].folder) { folders++; } else { files++; }
        });
        var any = files + folders > 0;
        $("#od-selection-count").text(any ? fmt(S.selected, {files: files, folders: folders}) : S.nothingSelected);
        $("#od-import, #od-clear").prop("disabled", !any);
    }

    $("#od-select-all").on("change", function () {
        var checked = this.checked;
        var filter = $("#od-filter").val().trim().toLowerCase();
        items.forEach(function (item) {
            if (item.importable && (!filter || item.name.toLowerCase().indexOf(filter) !== -1)) {
                if (checked) {
                    selection[item.id] = {id: item.id, name: item.name, folder: item.folder};
                } else {
                    delete selection[item.id];
                }
            }
        });
        renderSelection();
        renderList();
    });

    $("#od-clear").on("click", function () {
        selection = {};
        renderSelection();
        renderList();
    });

    $("#od-filter").on("input", renderList);

    $("#od-more").on("click", function () {
        if (cursor) { fetchPage({cursor: cursor}, true); }
    });

    $("#od-import").on("click", function () {
        var chosen = Object.keys(selection).map(function (id) { return selection[id]; });
        if (!chosen.length) { return; }
        var $btn = $(this).prop("disabled", true).text(S.importing);
        $.ajax({
            url: urls.importJobs,
            method: "POST",
            contentType: "application/json",
            dataType: "json",
            data: JSON.stringify({items: chosen})
        }).done(function () {
            selection = {};
            renderSelection();
            renderList();
            var $res = $("#od-import-result").empty().removeClass("hidden").text(S.queued + " ");
            $("<a>").attr("href", urls.tasks).addClass("alert-link").text(S.tasks).appendTo($res);
            loadJobs();
        }).fail(handleFailure).always(function () {
            $btn.text($btn.data("label"));
            renderSelection();
        });
    }).data("label", $("#od-import").text());

    // ── import jobs ──────────────────────────────────────────────────────

    function statusText(job) {
        switch (job.status) {
        case "queued": return S.status_queued;
        case "running": return job.enumerated
            ? fmt(S.status_running, {processed: job.processed, total: job.total})
            : S.status_scanning;
        case "done": return S.status_done;
        case "cancelled": return S.status_cancelled;
        default: return S.status_failed;
        }
    }

    function renderJobs(jobs) {
        var $body = $("#od-jobs tbody").empty();
        $("#od-jobs").toggleClass("hidden", !jobs.length);
        $("#od-no-jobs").toggleClass("hidden", jobs.length > 0);
        jobs.forEach(function (job) {
            var $tr = $("<tr>");
            $("<td>").text(job.created ? new Date(job.created + (/[zZ]|[+-]\d\d:\d\d$/.test(job.created) ? "" : "Z")).toLocaleString() : "").appendTo($tr);
            var names = job.selection.join(", ");
            if (job.selection_count > job.selection.length) {
                names += " " + fmt(S.more, {n: job.selection_count - job.selection.length});
            }
            $("<td class='od-job-selection'>").text(names).attr("title", names).appendTo($tr);
            var $status = $("<td>").text(statusText(job));
            if (job.error) { $status.attr("title", job.error).addClass("text-danger"); }
            if (job.current) {
                $("<div class='small text-muted od-job-detail'>").text(fmt(S.current_file, {name: job.current})).appendTo($status);
            }
            if (job.last_book) {
                $("<div class='small text-muted od-job-detail'>").text(fmt(S.last_book, {title: job.last_book})).appendTo($status);
            }
            $tr.append($status);
            $("<td>").text(job.imported).appendTo($tr);
            $("<td>").text(job.existing + job.duplicate).appendTo($tr);
            $("<td>").text(job.failed).appendTo($tr);
            var $actions = $("<td>");
            if (job.status === "queued" || job.status === "running") {
                $("<button type='button' class='btn btn-default btn-xs'>").text(S.cancel).on("click", function () {
                    $(this).prop("disabled", true);
                    $.ajax({url: urls.cancel.replace(/\/0\/cancel$/, "/" + job.id + "/cancel"), method: "POST"})
                        .fail(handleFailure).always(loadJobs);
                }).appendTo($actions);
            }
            $tr.append($actions);
            $body.append($tr);
        });
    }

    function loadJobs() {
        clearTimeout(jobsTimer);
        $.ajax({url: urls.jobs, dataType: "json"}).done(function (data) {
            var jobs = data.jobs || [];
            renderJobs(jobs);
            var active = jobs.some(function (j) { return j.status === "queued" || j.status === "running"; });
            jobsTimer = setTimeout(loadJobs, active ? 3000 : 30000);
        }).fail(function () {
            jobsTimer = setTimeout(loadJobs, 30000);
        });
    }

    renderSelection();
    loadFolder();
    loadJobs();
})();
