document.addEventListener("DOMContentLoaded", function() {
    var socket = io({transports: ['websocket']});
    socket.on('refresh', function() {
        location.reload();
    });

    let lastServerStart = null;
    let lastPlaylistUpdate = null;

    function checkServerRestart() {
        fetch('/server_status')
            .then(response => response.json())
            .then(data => {
                if (lastServerStart === null) {
                    lastServerStart = data.start_time;
                } else if (data.start_time !== lastServerStart) {
                    location.reload();
                }
                if (lastPlaylistUpdate === null) {
                    lastPlaylistUpdate = data.last_playlist_update;
                } else if (data.last_playlist_update !== lastPlaylistUpdate) {
                    location.reload();
                }
            })
            .catch(() => {});
    }
    setInterval(checkServerRestart, 5000);

    document.getElementById("toggleModeBtn")?.addEventListener("click", toggleMode);

    function toggleMode() {
        document.body.classList.toggle('light-mode');
        ["table", "th", "td", "a", "button", "img", "tr"].forEach(tag => {
            document.querySelectorAll(tag).forEach(el => el.classList.toggle('light-mode'));
        });
    }

    window.copyToClipboard = function(url) {
        if (navigator.clipboard) {
            navigator.clipboard.writeText(url).then(
                () => alert("Stream URL copied! Paste it in VLC or your preferred player."),
                () => fallbackCopy(url)
            );
        } else {
            fallbackCopy(url);
        }
    };

    function fallbackCopy(url) {
        var tempInput = document.createElement("textarea");
        tempInput.value = url;
        document.body.appendChild(tempInput);
        tempInput.select();
        try {
            document.execCommand("copy");
            alert("Stream URL copied! Paste it in VLC or your preferred player.");
        } catch (err) {
            alert("Failed to copy. Please copy manually.");
        }
        document.body.removeChild(tempInput);
    }
});