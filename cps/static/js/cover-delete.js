/* Calibre-Web Automated – quick delete ("🗑️") button on book covers.
 * Loaded only for users allowed to edit and delete books (see layout.html).
 */
(function ($) {
    "use strict";

    var BOOK_HREF = /\/book\/(\d+)\/?(?:[?#].*)?$/;
    var $modal = null;
    var pending = null;

    function bookIdFor($cover) {
        var $link = $cover.children("a[href]").first();
        var match = BOOK_HREF.exec($link.attr("href") || "");
        return match ? match[1] : null;
    }

    function bookTitleFor($cover) {
        var title = $cover.find("span.img").attr("title");
        if (!title) {
            title = $cover.closest(".book").find(".meta .title").first().attr("title");
        }
        return title || "";
    }

    function decorate(root) {
        $(root).find(".cover").addBack(".cover").filter(function () {
            return $(this).parent().hasClass("book");
        }).each(function () {
            var $cover = $(this);
            if ($cover.children(".cover-delete-btn").length) {
                return;
            }
            var bookId = bookIdFor($cover);
            if (!bookId) {
                return;
            }
            var label = $modal.data("label");
            $("<span/>", {
                "class": "cover-delete-btn",
                role: "button",
                tabindex: 0,
                title: label,
                "aria-label": label,
                "data-book-id": bookId,
                text: "\uD83D\uDDD1\uFE0F"
            }).appendTo($cover);
            $cover.addClass("has-cover-delete");
        });
    }

    function flash(type, message) {
        $(".navbar").after(
            $("<div/>", {"class": "row-fluid text-center"}).append(
                $("<div/>", {id: "flash_" + type, "class": "alert alert-" + type}).text(message)
            )
        );
    }

    function removeCard($book) {
        var $row = $book.parent();
        if ($row.data("isotope")) {
            $row.isotope("remove", $book).isotope("layout");
        } else {
            $book.fadeOut(200, function () { $book.remove(); });
        }
    }

    function askDelete($btn) {
        var $cover = $btn.closest(".cover");
        pending = {id: $btn.data("book-id"), $book: $cover.closest(".book")};
        $modal.find(".cover-delete-title").text(bookTitleFor($cover));
        $modal.modal("show");
    }

    function confirmDelete() {
        if (!pending) {
            return;
        }
        var target = pending;
        pending = null;
        $.ajax({
            method: "post",
            url: getPath() + "/ajax/delete/" + target.id,
            dataType: "json"
        }).done(function (data) {
            var items = $.isArray(data) ? data : [data];
            var failed = false;
            items.forEach(function (item) {
                if (!item || $.isEmptyObject(item)) {
                    return;
                }
                if (item.type === "danger") {
                    failed = true;
                }
                if (item.type !== "success") {
                    flash(item.type, String(item.message));
                }
            });
            if (!failed) {
                removeCard(target.$book);
            }
        }).fail(function () {
            flash("danger", $modal.data("error"));
        });
    }

    $(function () {
        $modal = $("#coverDeleteModal");
        if (!$modal.length) {
            return;
        }

        decorate(document.body);

        // Covers added later (infinite scroll, filters, ...).
        if (window.MutationObserver) {
            new MutationObserver(function (mutations) {
                mutations.forEach(function (m) {
                    for (var i = 0; i < m.addedNodes.length; i++) {
                        if (m.addedNodes[i].nodeType === 1) {
                            decorate(m.addedNodes[i]);
                        }
                    }
                });
            }).observe(document.body, {childList: true, subtree: true});
        }

        // Swallow every pointer event so cover links / quick actions never react.
        $(document).on("mousedown mouseup touchstart touchend", ".cover-delete-btn", function (e) {
            e.stopPropagation();
        });
        $(document).on("click", ".cover-delete-btn", function (e) {
            e.preventDefault();
            e.stopPropagation();
            askDelete($(this));
        });
        $(document).on("keydown", ".cover-delete-btn", function (e) {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                e.stopPropagation();
                askDelete($(this));
            }
        });
        $modal.on("click", "#coverDeleteConfirm", confirmDelete);
        $modal.on("hidden.bs.modal", function () { pending = null; });
    });
})(jQuery);
