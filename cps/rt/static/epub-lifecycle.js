/* SPDX-License-Identifier: GPL-3.0-or-later */
(function () {
  'use strict';
  var prototype = window.ePub.Contents.prototype;
  var resizeCheck = prototype.resizeCheck;
  var removeListeners = prototype.removeListeners;
  function stopResize(contents) {
    window.clearTimeout(contents.expanding);
    if (contents.rtResizeFrame !== undefined) {
      window.cancelAnimationFrame(contents.rtResizeFrame);
      contents.rtResizeFrame = undefined;
    }
  }
  prototype.resizeCheck = function () {
    if (!this.rtDisposed) return resizeCheck.apply(this, arguments);
  };
  prototype.resizeListeners = function () {
    if (this.rtDisposed) return;
    var contents = this;
    stopResize(contents);
    contents.rtResizeFrame = window.requestAnimationFrame(function () {
      contents.rtResizeFrame = undefined;
      contents.resizeCheck();
    });
    contents.expanding = window.setTimeout(function () { contents.resizeListeners(); }, 350);
  };
  prototype.visibilityListeners = function () {
    var contents = this;
    if (contents.rtDisposed || contents.rtVisibilityHandler) return;
    contents.rtVisibilityHandler = function () {
      if (contents.rtDisposed) return;
      if (document.visibilityState === 'visible') {
        if (contents.active === false) {
          contents.active = true;
          contents.resizeListeners();
        }
      } else {
        contents.active = false;
        stopResize(contents);
      }
    };
    document.addEventListener('visibilitychange', contents.rtVisibilityHandler, false);
  };
  prototype.removeListeners = function () {
    this.rtDisposed = true;
    stopResize(this);
    if (this.rtVisibilityHandler) {
      document.removeEventListener('visibilitychange', this.rtVisibilityHandler, false);
      this.rtVisibilityHandler = undefined;
    }
    return removeListeners.apply(this, arguments);
  };
}());
