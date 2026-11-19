/* SPDX-License-Identifier: GPL-3.0-or-later */
(function () {
    "use strict";

    const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
    const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);
    function model(data) {
        return {books: data.books.slice(), fields: data.fields.slice(), changes: {}, titles: {},
            numbering: false, start: "1", increment: "1", order: "selection", reordered: false,
            seriesSlots: data.books.map(book => book.values.series_index).sort((a, b) => Number(a) - Number(b)),
            originalOrder: data.books.map(book => book.id)};
    }
    function common(state, key) {
        const values = state.books.map(book => book.values[key]);
        return values.every(value => same(value, values[0])) ? values[0] : "";
    }
    function effective(state, book, key) {
        if (key === "title" && own(state.titles, book.id)) return state.titles[book.id];
        return own(state.changes, key) ? state.changes[key] : book.values[key];
    }
    function setTitle(state, id, value) {
        const book = state.books.find(item => item.id === id);
        const baseline = own(state.changes, "title") ? state.changes.title : book.values.title;
        if (value === baseline) delete state.titles[id];
        else state.titles[id] = value;
        if (state.order === "title") state.order = "custom";
    }
    function numberAt(state, book, index) {
        if (!state.numbering) return state.reordered && !own(state.changes, "series_index") ? state.seriesSlots[index] : effective(state, book, "series_index");
        return String(Math.round((Number(state.start) + index * Number(state.increment)) * 100) / 100);
    }
    function sortBooks(state, order) {
        state.order = order;
        state.reordered = order !== "selection";
        if (order === "selection") state.books.sort((a, b) => state.originalOrder.indexOf(a.id) - state.originalOrder.indexOf(b.id));
        if (order === "title") state.books.sort((a, b) => effective(state, a, "title").localeCompare(effective(state, b, "title")));
        if (order === "series") state.books.sort((a, b) => Number(a.values.series_index) - Number(b.values.series_index));
    }
    function moveBook(state, id, direction) {
        const index = state.books.findIndex(book => book.id === id);
        reorderBook(state, id, index + direction);
    }
    function reorderBook(state, id, target) {
        const index = state.books.findIndex(book => book.id === id);
        if (index < 0 || !Number.isInteger(target) || target < 0 || target >= state.books.length || target === index) return;
        state.books.splice(target, 0, state.books.splice(index, 1)[0]);
        state.order = "custom";
        state.reordered = true;
    }
    function payload(state) {
        return {book_ids: state.books.map(book => book.id), changes: state.changes, titles: state.titles,
            versions: Object.fromEntries(state.books.map(book => [String(book.id), book.version])),
            numbering: state.numbering ? {start: state.start, increment: state.increment} : null,
            reorder: state.reordered && !state.numbering && !own(state.changes, "series_index")};
    }
    const api = {model, common, effective, setTitle, numberAt, sortBooks, moveBook, reorderBook, payload};
    if (typeof module !== "undefined" && module.exports) module.exports = api;
    if (typeof document === "undefined") return;

    function start() {
        const configElement = document.getElementById("bulk-book-config");
        if (!configElement) return;
        const config = JSON.parse(configElement.textContent), text = config.text;
        const $id = id => document.getElementById(id);
        const modal = $id("bulkBookModal"), grid = document.querySelector("[data-bulk-book-grid]");
        if (!grid) return;
        const selected = new Set();
        let selecting = false, state = null, group = "series", busy = false, locked = false;
        let refreshOnClose = false, coverFile = null;
        const escape = value => String(value == null ? "" : value).replace(/[&<>"']/g,
            character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[character]));
        const format = (key, values = {}) => Object.keys(values).reduce(
            (value, name) => value.replace("%(" + name + ")s", String(values[name])), text[key]);
        const shown = value => Array.isArray(value) ? value.join(", ") : value === "" || value == null ? text.empty : String(value);
        const titleOf = book => effective(state, book, "title");
        const valueOf = key => own(state.changes, key) ? state.changes[key] : common(state, key);
        const fieldId = field => "bw-field-" + state.fields.indexOf(field);
        const error = message => {
            $id("bw-error").hidden = !message;
            $id("bw-error").textContent = message;
            if (message) $id("bw-error").focus();
        };
        const sortable = window.Sortable.create($id("bw-sequence"), {
            draggable: "tr[data-book]", handle: ".bw-drag-handle", animation: 150,
            ghostClass: "bw-sort-ghost", chosenClass: "bw-sort-chosen", dragClass: "bw-sort-drag",
            delayOnTouchOnly: true, delay: 150, fallbackTolerance: 4,
            onEnd(event) {
                if (!busy && !locked) reorderBook(state, Number(event.item.dataset.book), event.newIndex);
                render();
            }
        });

        function cards() {
            return Array.from(grid.querySelectorAll(".book")).map(card => {
                const link = card.querySelector(".book-cover-link[data-book-id]");
                return link ? {card, id: Number(link.dataset.bookId)} : null;
            }).filter(Boolean);
        }
        function decorate() {
            cards().forEach(({card, id}) => {
                if (card.querySelector(".bulk-cover-check")) return;
                const label = document.createElement("label"), input = document.createElement("input");
                label.className = "bulk-cover-check";
                input.type = "checkbox";
                input.dataset.bulkId = String(id);
                input.setAttribute("aria-label", format("selectBook", {id}));
                label.appendChild(input);
                card.querySelector(".cover").appendChild(label);
            });
            updateSelection();
        }
        function updateSelection() {
            grid.classList.toggle("bulk-selecting", selecting);
            cards().forEach(({card, id}) => {
                card.classList.toggle("bulk-book-selected", selecting && selected.has(id));
                const input = card.querySelector("[data-bulk-id]");
                if (input) input.checked = selected.has(id);
            });
            $id("bulk-book-actions").hidden = !selecting;
            $id("bulk-book-select").textContent = selected.size ? format("selectCount", {count: selected.size}) : text.select;
            $id("bulk-book-select").setAttribute("aria-pressed", String(selecting));
            $id("bulk-book-count").textContent = format("selected", {count: selected.size});
            $id("bulk-book-edit").disabled = !selected.size || busy;
            const ids = new Set(cards().filter(({card}) => card.offsetParent !== null).map(item => item.id));
            $id("bulk-book-all").checked = ids.size > 0 && Array.from(ids).every(id => selected.has(id));
            $id("bulk-book-all").indeterminate = selected.size > 0 && !$id("bulk-book-all").checked;
        }
        function toggle(id) {
            if (selected.has(id)) selected.delete(id);
            else if (selected.size < 100) selected.add(id);
            else $id("bulk-book-notice").textContent = text.limit;
            updateSelection();
        }
        function fieldHTML(field) {
            const value = valueOf(field.key), changed = own(state.changes, field.key);
            const mixed = !state.books.every(book => same(book.values[field.key], state.books[0].values[field.key]));
            const disabled = field.readonly || (field.key === "series_index" && state.numbering);
            const attributes = `id="${fieldId(field)}" data-field="${escape(field.key)}" ${disabled ? "disabled" : ""}`;
            let control;
            if (field.type === "textarea") control = `<textarea ${attributes} rows="6" placeholder="${escape(mixed ? text.mixed : text.empty)}">${escape(value)}</textarea>`;
            else if (["select", "multiple"].includes(field.type)) {
                const selectedValues = Array.isArray(value) ? value : [value];
                control = `<select ${attributes} ${field.type === "multiple" ? "multiple" : ""}>${field.type === "select" ? `<option value="">${escape(mixed ? text.mixed : text.empty)}</option>` : ""}${field.options.map(option => `<option value="${escape(option.value)}" ${selectedValues.includes(option.value) ? "selected" : ""}>${escape(option.label)}</option>`).join("")}</select>`;
            } else control = `<input ${attributes} type="${escape(field.type)}" value="${escape(value)}" placeholder="${escape(mixed ? text.mixed : text.empty)}" ${["min", "max", "step"].filter(key => own(field, key)).map(key => key + '="' + field[key] + '"').join(" ")}>`;
            const help = field.key === "title" ? text.sharedTitleHelp : field.key === "authors" ? text.authorHelp :
                ["tags", "languages"].includes(field.key) ? text.listHelp : field.key === "shelves" ? text.shelfHelp : field.help || "";
            return `<div class="bw-field ${field.wide ? "wide" : ""} ${changed ? "changed" : ""}"><div class="bw-field-head"><label for="${fieldId(field)}">${escape(field.label)}</label><span>${changed ? `<button type="button" class="bw-link" data-reset="${escape(field.key)}">${escape(text.reset)}</button>` : ""}${!disabled && field.clearable !== false ? `<button type="button" class="bw-link" data-clear="${escape(field.key)}">${escape(text.clear)}</button>` : ""}</span></div><small>${escape(field.readonly ? text.readonly : mixed ? text.mixedLabel : text.common)}</small>${control}<small>${escape(changed ? text.changed : "")} ${escape(help)}</small></div>`;
        }
        function numberingHTML() {
            return `<section class="bw-numbering"><label><input type="checkbox" id="bw-number" ${state.numbering ? "checked" : ""}> ${escape(text.autoNumber)}</label><div class="bw-number-controls"><label>${escape(text.start)}<input id="bw-start" type="number" min="0" step="0.01" value="${escape(state.start)}" ${!state.numbering ? "disabled" : ""}></label><label>${escape(text.increment)}<input id="bw-increment" type="number" min="0.01" step="0.01" value="${escape(state.increment)}" ${!state.numbering ? "disabled" : ""}></label><label>${escape(text.order)}<select id="bw-order"><option value="selection" ${state.order === "selection" ? "selected" : ""}>${escape(text.selectionOrder)}</option><option value="title" ${state.order === "title" ? "selected" : ""}>${escape(text.titleOrder)}</option><option value="series" ${state.order === "series" ? "selected" : ""}>${escape(text.seriesOrder)}</option><option value="custom" ${state.order === "custom" ? "selected" : ""}>${escape(text.customOrder)}</option></select></label></div></section><p class="bw-hint">${escape(text.numberHelp)}</p>`;
        }
        function systemHTML(book) {
            const values = [[text.id, book.id], [text.uuid, book.system.uuid], [text.added, book.system.added],
                [text.modified, book.system.modified], [text.formats, book.system.formats],
                [text.progress, book.system.progress == null ? "" : book.system.progress + "%"],
                [text.progressUpdated, book.system.progress_updated]];
            return values.map(([label, value]) => `<div><dt>${escape(label)}</dt><dd>${escape(shown(value))}</dd></div>`).join("");
        }
        function editorHTML() {
            let html = `<p class="bw-hint">${escape(text.sharedHint)}</p><h3>${escape(config.groups.find(item => item.id === group).label)}</h3>`;
            const fields = state.fields.filter(field => field.group === group);
            if (group === "system") return html + `<p class="bw-hint">${escape(text.systemHelp)}</p>` + state.books.map(book => `<details class="bw-preview" open><summary>${escape(titleOf(book))}</summary><dl class="bw-values">${systemHTML(book)}</dl></details>`).join("");
            html += `<div class="bw-field-grid">${fields.map(fieldHTML).join("")}</div>`;
            if (group === "series") html += numberingHTML();
            if (group === "custom" && !fields.length) html += `<p class="bw-hint">${escape(text.noCustom)}</p>`;
            if (group === "details") html += `<h3>${escape(text.identifiers)}</h3><p class="bw-hint">${escape(text.identifierHelp)}</p><div class="bw-field-grid">${state.fields.filter(field => field.group === "identifiers").map(fieldHTML).join("")}</div><label for="bw-identifier-type">${escape(text.identifierType)}</label><div class="bw-add-identifier"><input id="bw-identifier-type" type="text" placeholder="ISBN, ASIN, Goodreads"><button type="button" class="btn btn-default" id="bw-add-identifier">${escape(text.addIdentifier)}</button></div>`;
            if (group === "content") html += `<p class="bw-hint">${escape(text.coverHelp)}</p><label class="bw-cover-upload" for="bw-cover-file">${escape(text.uploadCover)}<input type="file" id="bw-cover-file" accept="image/jpeg,image/png,image/webp"></label>${coverFile ? `<p>${escape(coverFile.name)} <button type="button" class="bw-link" id="bw-reset-cover">${escape(text.resetCover)}</button></p>` : ""}`;
            return html;
        }
        function render() {
            const focus = document.activeElement, focusId = focus && focus.id, cursor = focus && focus.selectionStart;
            const scrollLeft = $id("bw-editor").scrollTop, scrollRight = modal.querySelector(".bw-results").scrollTop;
            const opened = new Set(Array.from($id("bw-preview").querySelectorAll("details[open]")).map(item => item.dataset.book));
            $id("bulkBookHeading").textContent = format("editBooks", {count: state.books.length});
            $id("bw-tabs").innerHTML = config.groups.map(item => `<button type="button" class="bw-tab ${item.id === group ? "active" : ""}" data-group="${item.id}" aria-current="${item.id === group ? "page" : "false"}">${escape(item.label)}</button>`).join("");
            $id("bw-editor").innerHTML = editorHTML();
            $id("bw-number-state").textContent = state.numbering ? text.numberOn : text.numberOff;
            $id("bw-order-notice").textContent = state.reordered && !state.numbering && new Set(state.seriesSlots.map(Number)).size < state.books.length ? text.duplicateNumbers : "";
            $id("bw-sequence").innerHTML = state.books.map((book, index) => {
                const changed = own(state.titles, book.id), title = titleOf(book);
                return `<tr data-book="${book.id}"><td class="bw-grip-cell"><span class="bw-drag-handle" role="img" aria-label="${escape(format("dragBook", {title}))}" title="${escape(text.dragHelp)}"><svg width="12" height="24" viewBox="0 0 12 24" aria-hidden="true"><circle cx="3" cy="6" r="1.5"/><circle cx="9" cy="6" r="1.5"/><circle cx="3" cy="12" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="3" cy="18" r="1.5"/><circle cx="9" cy="18" r="1.5"/></svg></span></td><td><div class="bw-title ${changed ? "changed" : ""}"><label for="bw-title-${book.id}">${escape(text.individualTitle)}</label><input id="bw-title-${book.id}" type="text" data-title="${book.id}" value="${escape(title)}" aria-label="${escape(format("titleFor", {id: book.id}))}" ${!title.trim() ? 'aria-invalid="true"' : ""}><small>${escape(changed ? text.onlyThis : text.titleHelp)}</small>${changed ? `<small>${escape(format("was", {title: book.values.title}))}</small><button type="button" class="bw-link" data-reset-title="${book.id}">${escape(text.resetTitle)}</button>` : ""}</div></td><td>${escape(shown(book.values.series_index))}</td><td>${escape(shown(numberAt(state, book, index)))}</td><td><button type="button" id="bw-up-${book.id}" class="bw-move" data-move="${book.id}" data-direction="-1" aria-label="${escape(text.up + ': ' + title)}" ${index === 0 ? "disabled" : ""}>${escape(text.up)}</button><button type="button" id="bw-down-${book.id}" class="bw-move" data-move="${book.id}" data-direction="1" aria-label="${escape(text.down + ': ' + title)}" ${index === state.books.length - 1 ? "disabled" : ""}>${escape(text.down)}</button></td></tr>`;
            }).join("");
            const changes = state.fields.filter(field => own(state.changes, field.key)).map(field => `${field.label}: ${shown(state.changes[field.key])}`);
            state.books.filter(book => own(state.titles, book.id)).forEach(book => changes.push(`${format("titleFor", {id: book.id})}: ${book.values.title} -> ${state.titles[book.id]}`));
            if (state.numbering) changes.push(`${text.autoNumber}: ${state.start} / +${state.increment}`);
            else if (state.books.some((book, index) => Number(numberAt(state, book, index)) !== Number(effective(state, book, "series_index")))) changes.push(text.reassignNumbers);
            if (coverFile) changes.push(text.coverFile + ": " + coverFile.name);
            $id("bw-changes").innerHTML = changes.length ? changes.map(change => `<p>${escape(change)}</p>`).join("") : `<p>${escape(text.noChanges)}</p>`;
            $id("bw-preview").innerHTML = state.books.map((book, index) => `<details class="bw-preview" data-book="${book.id}" ${opened.has(String(book.id)) ? "open" : ""}><summary><img class="bw-cover" src="${escape(book.cover)}" alt="${escape(text.cover)}">${escape(titleOf(book))}<br><small>${escape(effective(state, book, "authors"))}</small></summary><dl class="bw-values">${state.fields.map(field => `<div><dt>${escape(field.label)}</dt><dd>${escape(shown(field.key === "series_index" ? numberAt(state, book, index) : effective(state, book, field.key)))}</dd></div>`).join("")}${systemHTML(book)}</dl></details>`).join("");
            $id("bw-count").textContent = format("fieldChanges", {count: Object.keys(state.changes).length}) + " / " + format("titleChanges", {count: Object.keys(state.titles).length});
            $id("bw-save").textContent = busy ? text.saving : format("apply", {count: state.books.length});
            $id("bw-save").disabled = busy || locked || !changes.length;
            modal.setAttribute("aria-busy", String(busy));
            sortable.option("disabled", busy || locked);
            modal.querySelectorAll(".bw-layout input,.bw-layout textarea,.bw-layout select,.bw-layout button,.bw-tabs button").forEach(element => { if (busy || locked) element.disabled = true; });
            $id("bw-editor").scrollTop = scrollLeft;
            modal.querySelector(".bw-results").scrollTop = scrollRight;
            if (focusId && $id(focusId)) {
                const replacement = $id(focusId);
                replacement.focus({preventScroll: true});
                if (cursor != null && ["text", "url", "textarea"].includes(replacement.type)) replacement.setSelectionRange(cursor, cursor);
            }
        }
        async function load() {
            busy = true;
            updateSelection();
            $id("bulk-book-notice").textContent = text.loading;
            try {
                const response = await fetch(config.url + "?ids=" + Array.from(selected).join(","), {credentials: "same-origin"});
                if (!response.headers.get("content-type")?.includes("application/json")) throw new Error(text.loadError);
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || text.loadError);
                state = model(data);
                const types = new Set(data.books.flatMap(book => Object.keys(book.identifiers)));
                types.forEach(type => {
                    state.fields.push({key: "identifier:" + type, label: type.toUpperCase(), group: "identifiers", type: "text"});
                    state.books.forEach(book => book.values["identifier:" + type] = book.identifiers[type] || "");
                });
                group = "series";
                coverFile = null;
                locked = false;
                refreshOnClose = false;
                busy = false;
                error("");
                $id("bw-outcome").hidden = true;
                $id("bw-cancel").textContent = text.cancel;
                render();
                window.jQuery(modal).modal({backdrop: "static", show: true});
                $id("bulk-book-notice").textContent = "";
            } catch (failure) {
                $id("bulk-book-notice").textContent = failure.message || text.loadError;
            } finally {
                busy = false;
                updateSelection();
            }
        }
        async function save() {
            if (busy || locked) return;
            const untitled = state.books.find(book => !titleOf(book).trim());
            if (untitled) { error(format("titleRequired", {id: untitled.id})); return; }
            const invalid = Array.from(modal.querySelectorAll("input,select,textarea")).find(element => !element.disabled && !element.checkValidity());
            if (invalid) { invalid.reportValidity(); return; }
            const body = new FormData();
            body.append("payload", JSON.stringify(payload(state)));
            if (coverFile) body.append("cover_file", coverFile);
            busy = true;
            error("");
            render();
            try {
                const response = await fetch(config.url, {method: "POST", credentials: "same-origin", body,
                    headers: {"X-CSRFToken": document.querySelector("input[name=csrf_token]").value}});
                if (!response.headers.get("content-type")?.includes("application/json")) throw new Error(text.saveError);
                const data = await response.json();
                if (response.status === 400 && data.error) { error(data.error); return; }
                if (!Array.isArray(data.results)) throw new Error(text.saveError);
                locked = true;
                refreshOnClose = true;
                $id("bw-outcome").hidden = false;
                $id("bw-outcome").textContent = (data.success ? text.finished : text.partial) + "\n" + data.results.map(result =>
                    "#" + result.id + ": " + (result.status === "saved" ? text.saved : result.status === "unchanged" ? text.unchanged : result.status === "skipped" ? text.skipped : text.error) +
                    (result.error ? " - " + result.error : "") + (result.warning ? "\n" + result.warning : "") +
                    (result.warnings?.length ? "\n" + result.warnings.join("\n") : "")).join("\n");
                $id("bw-cancel").textContent = text.reload;
                $id("bw-outcome").scrollIntoView({block: "nearest"});
            } catch (failure) {
                locked = true;
                refreshOnClose = true;
                $id("bw-cancel").textContent = text.reload;
                error(text.saveError);
            } finally {
                busy = false;
                render();
            }
        }

        $id("bulk-book-select").addEventListener("click", () => { selecting = !selecting; if (!selecting) selected.clear(); updateSelection(); });
        $id("bulk-book-clear").addEventListener("click", () => { selected.clear(); updateSelection(); });
        $id("bulk-book-all").addEventListener("change", event => {
            const ids = new Set(cards().filter(({card}) => card.offsetParent !== null).map(item => item.id));
            if (event.target.checked) {
                if (new Set([...selected, ...ids]).size > 100) $id("bulk-book-notice").textContent = text.limit;
                else ids.forEach(id => selected.add(id));
            } else ids.forEach(id => selected.delete(id));
            updateSelection();
        });
        grid.addEventListener("click", event => {
            if (!selecting) return;
            if (event.target.closest(".bulk-cover-check")) { event.stopPropagation(); return; }
            const card = event.target.closest(".book"), link = card?.querySelector(".book-cover-link[data-book-id]");
            if (!link) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            toggle(Number(link.dataset.bookId));
        }, true);
        grid.addEventListener("change", event => { if (event.target.dataset.bulkId) toggle(Number(event.target.dataset.bulkId)); });
        $id("bulk-book-edit").addEventListener("click", load);
        modal.addEventListener("click", event => {
            const button = event.target.closest("button");
            if (!button || busy || locked) return;
            if (button.dataset.group) { group = button.dataset.group; error(""); render(); $id("bw-editor").scrollTop = 0; }
            if (button.dataset.reset) { delete state.changes[button.dataset.reset]; error(""); render(); }
            if (button.dataset.clear) { const field = state.fields.find(item => item.key === button.dataset.clear); state.changes[field.key] = field.type === "multiple" ? [] : ""; if (field.key === "cover_url") coverFile = null; render(); }
            if (button.dataset.resetTitle) { delete state.titles[button.dataset.resetTitle]; error(""); render(); }
            if (button.dataset.move) { moveBook(state, Number(button.dataset.move), Number(button.dataset.direction)); render(); }
            if (button.id === "bw-save") save();
            if (button.id === "bw-reset-cover") { coverFile = null; render(); }
            if (button.id === "bw-add-identifier") {
                const type = $id("bw-identifier-type").value.trim().toLowerCase(), key = "identifier:" + type;
                if (!/^[a-z][a-z0-9_-]{0,63}$/.test(type) || state.fields.some(field => field.key === key)) { error(text.invalidIdentifier); return; }
                state.fields.push({key, label: type.toUpperCase(), group: "identifiers", type: "text"});
                state.books.forEach(book => book.values[key] = "");
                render();
                $id(fieldId(state.fields[state.fields.length - 1])).focus();
            }
        });
        function fieldChanged(event) {
            if (busy || locked) return;
            const element = event.target;
            if (element.dataset.title) setTitle(state, Number(element.dataset.title), element.value);
            else if (element.dataset.field) {
                state.changes[element.dataset.field] = element.multiple ? Array.from(element.selectedOptions).map(option => option.value).sort() : element.value;
                if (element.dataset.field === "cover_url") coverFile = null;
                if (element.dataset.field === "title" && state.order === "title") state.order = "custom";
            } else if (element.id === "bw-number") state.numbering = element.checked;
            else if (element.id === "bw-start") state.start = element.value;
            else if (element.id === "bw-increment") state.increment = element.value;
            else if (element.id === "bw-order") sortBooks(state, element.value);
            else if (element.id === "bw-cover-file") {
                const file = element.files[0];
                if (!file) return;
                if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) { error(text.invalidCover); return; }
                coverFile = file;
                delete state.changes.cover_url;
            } else return;
            error("");
            render();
        }
        modal.addEventListener("input", event => { if (!["SELECT", "BUTTON"].includes(event.target.tagName) && event.target.type !== "file" && event.target.type !== "checkbox") fieldChanged(event); });
        modal.addEventListener("change", event => { if (event.target.tagName === "SELECT" || ["file", "checkbox"].includes(event.target.type)) fieldChanged(event); });
        window.jQuery(modal).on("hide.bs.modal", event => { if (busy) event.preventDefault(); });
        window.jQuery(modal).on("hidden.bs.modal", () => { if (refreshOnClose) window.location.reload(); });
        new MutationObserver(mutations => { if (mutations.some(mutation => mutation.addedNodes.length)) decorate(); }).observe(grid, {childList: true, subtree: true});
        decorate();
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
})();
