// Phase 1 + Phase 2 CCP integration
// =================================
// - Embeds CCP iframe in #ccpContainer
// - Auto-opens patient chart on contact.onConnecting
// - Phase 2: polls /api/streaming/session/<id>/live-transcript every 2s
//   while contact is connected, renders segments via addTranscriptEntry()
// - On contact.onEnded, schedules SOAP/codes refresh via fetchAndDisplayStreamingOutputs()

(function () {
    'use strict';

    // Connect Workspace event handler for embedded third-party apps
    window.addEventListener('message', function(event) {
        // Reset stale state on each new page load
        if (!window._embedStateReset) {
            // Check if consultation recently ended (persisted in localStorage)
            // NOTE: window._patientOpened is now an IDENTITY gate (holds the specific
            // contactId whose patient chart is considered "open"), not a boolean.
            // The gate check below (line ~85ish) is `contactId !== window._patientOpened`,
            // so it only blocks re-opening the SAME contact that just ended — it never
            // blocks a genuinely new/different call. Previously this used the magic
            // string 'blocked', which compared unequal to every real contactId anyway,
            // so it silently blocked ALL calls (not just the stale one) for 5 minutes
            // after any call ended — including completely unrelated new calls. Confirmed
            // live: a second real call placed 39s after the first ended got zero patient-
            // chart / patient-insights activity at all because of this.
            var consultEndedTs = null;
            var lastEndedContactId = null;
            try {
                consultEndedTs = parseInt(localStorage.getItem('consultationEnded'));
                lastEndedContactId = localStorage.getItem('lastEndedContactId');
            } catch(e) {}
            if (consultEndedTs && (Date.now() - consultEndedTs) < 300000 && lastEndedContactId) {
                // Consultation ended within last 5 min — block only THAT specific contact
                window._consultationEnded = true;
                window._patientOpened = lastEndedContactId;
                console.log('[CCP-EMBED] Consultation recently ended — blocking reopen of', lastEndedContactId, '(new contacts are unaffected)');
            } else {
                // Clear stale consultation flag
                try { localStorage.removeItem('consultationEnded'); localStorage.removeItem('lastEndedContactId'); } catch(e) {}
                window._patientOpened = null;
                window._contactHandled = null;
            }
            window._embedStateReset = true;
        }
        // Only accept messages from Connect
        if (!event.origin.includes('connect') && !event.origin.includes('amazonaws.com')) return;
        
        var data = event.data;
        console.log('[CCP-EMBED] Message from Connect:', JSON.stringify(data).substring(0, 500));
        
        if (!data || !data.event) return;
        
        switch(data.event) {
            case 'acknowledge':
                // Respond to Connect's handshake
                console.log('[CCP-EMBED] Sending acknowledge response to Connect');
                event.source.postMessage({ event: 'acknowledge', data: null }, event.origin);
                break;
                
            case 'update':
            case 'contact':
            case 'contactUpdate':
                // Contact data received from Connect
                console.log('[CCP-EMBED] Contact event received:', JSON.stringify(data.data).substring(0, 500));
                if (data.data && data.data.contactId) {
                    handleConnectContactEvent(data.data);
                }
                break;
                
            case 'init':
            case 'initialized':
                console.log('[CCP-EMBED] Connect workspace initialized');
                break;
                
            case 'agent::update':
                // Agent state update — contains contacts in snapshot
                try {
                    var snapshot = data.data && data.data.snapshot;
                    if (snapshot && snapshot.contacts && snapshot.contacts.length > 0) {
                        snapshot.contacts.forEach(function(contact) {
                            var state = contact.state && contact.state.type;
                            var contactId = contact.contactId;
                            console.log('[CCP-EMBED] Contact in snapshot:', contactId, 'state:', state);
                            
                            if ((state === 'connecting' || state === 'connected') && contactId && !window._consultationEnded) {
                                // Extract patient_id from contact attributes
                                var attrs = contact.attributes || contact.contactAttributes || {};
                                var patientId = null;
                                
                                // Attributes might be {key: {value: "xxx"}} or {key: "xxx"}
                                if (attrs.patient_id) {
                                    patientId = attrs.patient_id.value || attrs.patient_id;
                                }
                                
                                console.log('[CCP-EMBED] Contact attributes:', JSON.stringify(attrs).substring(0, 300));
                                console.log('[CCP-EMBED] Patient ID:', patientId);
                                
                                if (patientId && contactId !== window._patientOpened) {
                                    // If previous consultation ended, reset UI first
                                    if (window._consultationEnded) {
                                        console.log('[CCP-EMBED] New call — resetting from previous consultation');
                                        var scheduleScreen = document.getElementById('scheduleScreen');
                                        if (scheduleScreen) scheduleScreen.classList.remove('hidden');
                                        var patientPortal = document.querySelector('.patient-portal-background');
                                        if (patientPortal) patientPortal.style.display = 'none';
                                        var previsitContainer = document.getElementById('previsitContainer');
                                        if (previsitContainer) previsitContainer.classList.remove('active');
                                        var soapOverlay = document.getElementById('soapNotesOverlay');
                                        if (soapOverlay) { soapOverlay.classList.remove('active'); soapOverlay.style.opacity = '0'; soapOverlay.style.visibility = 'hidden'; }
                                        window._consultationEnded = false;
                                        try { localStorage.removeItem('consultationEnded'); } catch(e) {}
                                    }

                                    window._patientOpened = contactId; // Tracks which contact opened a patient
                                    window._contactHandled = contactId;
                                    window.currentStreamingSessionId = contactId;
                                    console.log('[CCP-EMBED] Patient ID extracted:', patientId);
                                    // Write to localStorage so display iframe can pick it up
                                    try {
                                        localStorage.setItem('activePatient', JSON.stringify({
                                            patientId: patientId,
                                            contactId: contactId,
                                            timestamp: Date.now()
                                        }));
                                        console.log('[CCP-EMBED] Wrote activePatient to localStorage');
                                    } catch(e) { console.warn('[CCP-EMBED] localStorage write failed:', e); }
                                    // Also try calling openPatient directly (works if same iframe)
                                    if (typeof window.openPatient === 'function') {
                                        console.log('[CCP-EMBED] Opening patient directly:', patientId);
                                        window.openPatient(patientId);
                                        startTranscriptPolling(contactId);
                                    }
                                }
                            }
                            
                            // Detect call ended — schedule SOAP/codes refresh
                            if (state === 'ended' && contactId && contactId === window._contactHandled) {
                                console.log('[CCP-EMBED] Contact ended:', contactId);
                                stopTranscriptPolling(contactId);
                                // The live-transcript poller was actually started (in main.js,
                                // startConsultationRecording) using the bridge's own scribeSessionId,
                                // NOT this Connect contactId — they are different ID values. Without
                                // stopping the scribeSessionId-keyed poller too, it runs forever, and
                                // fetchAndDisplayStreamingOutputs(contactId) would query the wrong
                                // session (empty), which is why the Coding Insights box stayed empty.
                                var scribeSessionId = window._activeScribeSessionId;
                                if (scribeSessionId && scribeSessionId !== contactId) {
                                    stopTranscriptPolling(scribeSessionId);
                                }
                                var outputsSessionId = scribeSessionId || contactId;
                                if (typeof window.fetchAndDisplayStreamingOutputs === 'function') {
                                    setTimeout(function() { window.fetchAndDisplayStreamingOutputs(outputsSessionId); }, 30000);
                                    setTimeout(function() { window.fetchAndDisplayStreamingOutputs(outputsSessionId); }, 60000);
                                    setTimeout(function() { window.fetchAndDisplayStreamingOutputs(outputsSessionId); }, 90000);
                                }
                                window._contactHandled = null;
                                window._activeScribeSessionId = null;
                                // _patientOpened intentionally stays set to this contactId (not
                                // reset to null) — it's now an identity gate, not a boolean, so
                                // leaving it as this specific contactId still correctly blocks a
                                // stale re-echo of THIS SAME contact, while a genuinely new call
                                // with a different contactId passes the `contactId !== window.
                                // _patientOpened` check immediately, without waiting 5 minutes.
                                // Persist which contact this is, so the block survives a page
                                // reload (the page-load reset block above reads this back).
                                window._consultationEnded = true;
                                try {
                                    localStorage.setItem('consultationEnded', Date.now().toString());
                                    localStorage.setItem('lastEndedContactId', contactId);
                                } catch(e) {}
                            }
                        });
                    }
                } catch(e) {
                    console.warn('[CCP-EMBED] Error parsing agent::update:', e);
                }
                break;
                
            case 'contact::view':
                // Connect tells us a contact is being viewed/active
                console.log('[CCP-EMBED] contact::view received, contactId:', data.data && data.data.contactId);
                var viewContactId = data.data && data.data.contactId;
                if (!viewContactId && window._consultationEnded) {
                    // Empty contactId — don't reset yet, wait for next call
                    console.log('[CCP-EMBED] Contact cleared — staying on results until next call');
                }
                if (viewContactId && !window._contactHandled && !window._consultationEnded) {
                    window._contactHandled = viewContactId;
                    window.currentStreamingSessionId = viewContactId;
                    // Use patient_id from localStorage (set by the snapshot handler above)
                    // or fall back to matching by patient_id from activePatient
                    var activePatientData = null;
                    try { activePatientData = JSON.parse(localStorage.getItem('activePatient')); } catch(e) {}
                    
                    if (activePatientData && activePatientData.patientId && typeof window.openPatient === 'function') {
                        // Open the correct patient from contact attributes
                        console.log('[CCP-EMBED] contact::view — opening patient from attributes:', activePatientData.patientId);
                        window.openPatient(activePatientData.patientId);
                        startTranscriptPolling(viewContactId);
                    } else {
                        // Fallback: find patient by matching PATIENT_ID_MAP values
                        var patientKeys = Object.keys(window.PATIENT_ID_MAP || {});
                        var matchedKey = null;
                        if (activePatientData && activePatientData.patientId) {
                            for (var i = 0; i < patientKeys.length; i++) {
                                if (window.PATIENT_ID_MAP[patientKeys[i]] === activePatientData.patientId) {
                                    matchedKey = patientKeys[i];
                                    break;
                                }
                            }
                        }
                        if (matchedKey && typeof window.openPatient === 'function') {
                            console.log('[CCP-EMBED] contact::view — opening matched patient:', matchedKey);
                            window.openPatient(matchedKey);
                            startTranscriptPolling(viewContactId);
                        } else if (patientKeys.length > 0 && typeof window.openPatient === 'function') {
                            // Last resort: open first patient
                            console.log('[CCP-EMBED] contact::view — no match, opening first patient');
                            window.openPatient(patientKeys[0]);
                            startTranscriptPolling(viewContactId);
                        }
                    }
                }
                break;
                
            default:
                console.log('[CCP-EMBED] Unknown event:', data.event);
        }
    });
    
    function handleConnectContactEvent(contactData) {
        console.log('[CCP-EMBED] Processing contact:', contactData.contactId);
        var patientId = null;
        
        // Try to extract patient_id from contact attributes
        if (contactData.attributes && contactData.attributes.patient_id) {
            patientId = contactData.attributes.patient_id;
        } else if (contactData.contactAttributes && contactData.contactAttributes.patient_id) {
            patientId = contactData.contactAttributes.patient_id;
        }
        
        if (patientId && typeof window.openPatient === 'function') {
            console.log('[CCP-EMBED] Opening patient:', patientId);
            window.openPatient(patientId);
            window.currentStreamingSessionId = contactData.contactId;
        }
    }
    // Set window.CONNECT_CCP_URL in frontend/js/config.js to your own instance.
    const CCP_URL = window.CONNECT_CCP_URL || 'https://your-instance-alias.my.connect.aws/ccp-v2';
    const BACKEND_URL = window.BACKEND_URL || 'http://localhost:5000';
    const TRANSCRIPT_POLL_INTERVAL_MS = 2000;

    let initialized = false;
    let activePollers = {}; // sessionId -> intervalId
    let renderedSegmentKeys = {}; // sessionId -> Set of "ts:text" keys we've already rendered

    function init() {
        if (initialized) return;
        // Wait for the Streams library to load (script tag in index.html)
        if (typeof window.connect === 'undefined' || !window.connect.core) {
            setTimeout(init, 250);
            return;
        }
        var isEmbedded = (window.self !== window.top);
        if (isEmbedded) {
            // In embedded mode, DON'T call initCCP — it conflicts with workspace's own CCP.
            // The postMessage event listener (top of file) handles contact detection.
            // Transcript polling is triggered from startConsultationRecording via /api/active-session.
            initialized = true;
            console.log('[CCP] Embedded mode — using postMessage handler only (no initCCP)');
            return;
        }
        // Standalone mode — embed CCP ourselves
        var c = document.getElementById('ccpContainer');
        if (!c) {
            setTimeout(init, 250);
            return;
        }
        try {
            window.connect.core.initCCP(c, {
                ccpUrl: CCP_URL,
                loginPopup: true,
                loginPopupAutoClose: true,
                softphone: { allowFramedSoftphone: true }
            });
            initialized = true;
            console.log('[CCP] init done (standalone mode)');
            window.connect.contact(handleContact);
        } catch (e) {
            console.error('[CCP] init failed', e);
        }
    }

    function handleContact(contact) {
        const cid = contact.getContactId();
        console.log('[CCP] contact', cid);
        let opened = false;

        contact.onConnecting(function () {
            if (opened) return;
            const attrs = contact.getAttributes() || {};
            const pid = attrs.patient_id && attrs.patient_id.value;
            if (!pid) {
                console.warn('[CCP] no patient_id');
                return;
            }
            console.log('[CCP] opening patient', pid);
            if (typeof window.openPatient === 'function') {
                try {
                    window.openPatient(pid);
                    opened = true;
                } catch (e) {
                    console.error('[CCP] openPatient failed', e);
                }
            }
            window.currentStreamingSessionId = cid;
        });

        contact.onConnected(function () {
            console.log('[CCP] connected — starting live transcript polling for', cid);
            startTranscriptPolling(cid);
            // Auto-open consultation overlay + transcript panel so transcripts are visible
            setTimeout(function () {
                try {
                    if (typeof window.startConsultation === 'function') {
                        console.log('[CCP] auto-opening consultation overlay');
                        window.startConsultation();
                    }
                    setTimeout(function () {
                        var panel = document.getElementById('transcriptPanel');
                        var alreadyOpen = panel && panel.style.display !== 'none';
                        if (typeof window.toggleScribeTranscript === 'function' && !window.scribeTranscriptVisible) {
                            console.log('[CCP] auto-opening transcript panel');
                            window.toggleScribeTranscript();
                        }
                    }, 800);
                } catch (e) {
                    console.warn('[CCP] auto-open consultation failed', e);
                }
            }, 500);
        });

        contact.onEnded(function () {
            // Guard: in embedded mode, onEnded can fire immediately for stale contacts
            // Only stop if polling has been active for at least 5 seconds
            var pollerAge = (window._pollerStartTimes && window._pollerStartTimes[cid]) ? (Date.now() - window._pollerStartTimes[cid]) : 99999;
            if (pollerAge < 5000) {
                console.log("[CCP] onEnded fired too quickly (" + pollerAge + "ms) - ignoring for", cid);
                return;
            }
            console.log('[CCP] contact ended', cid);
            stopTranscriptPolling(cid);
            // Schedule SOAP/codes refresh after ConnectHealth post-stream actions complete
            // After data renders, ensure the SOAP overlay is visible.
            function _showSoap() {
                var s = document.getElementById('soapNotesOverlay');
                if (s) {
                    s.classList.add('active');
                    s.style.opacity = '1';
                    s.style.visibility = 'visible';
                    s.style.pointerEvents = 'auto';
                    console.log('[CCP] SOAP overlay forced visible');
                }
            }
            if (typeof window.fetchAndDisplayStreamingOutputs === 'function') {
                setTimeout(function () {
                    window.fetchAndDisplayStreamingOutputs(cid).then(_showSoap).catch(_showSoap);
                }, 30000);
                setTimeout(function () {
                    window.fetchAndDisplayStreamingOutputs(cid).then(_showSoap).catch(_showSoap);
                }, 60000);
                setTimeout(function () {
                    window.fetchAndDisplayStreamingOutputs(cid).then(_showSoap).catch(_showSoap);
                }, 90000);
            }
        });
    }

    window.startTranscriptPollingGlobal = startTranscriptPolling;
    function startTranscriptPolling(sessionId) {
        // Avoid duplicates if already polling
        if (activePollers[sessionId]) return;
        renderedSegmentKeys[sessionId] = new Set();

        const tick = function () {
            fetch(`${BACKEND_URL}/api/streaming/session/${encodeURIComponent(sessionId)}/live-transcript`)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data || !data.success) return;
                    const segs = data.segments || [];
                    const seen = renderedSegmentKeys[sessionId];
                    if (!seen) return; // polling was stopped during fetch
                    for (let i = 0; i < segs.length; i++) {
                        const seg = segs[i];
                        // Dedupe key: ts:text:final — final updates should re-render replacing partial
                        const key = seg.ts + ':' + seg.text;
                        if (seen.has(key)) continue;
                        seen.add(key);
                        // Render via existing transcript function in main.js
                        if (typeof window.addTranscriptEntry === 'function') {
                            try {
                                window.addTranscriptEntry(seg.text, !!seg.final);
                            } catch (e) {
                                console.warn('[CCP] addTranscriptEntry failed', e);
                            }
                        } else {
                            console.log('[CCP] transcript:', seg.text, seg.final ? '(final)' : '(partial)');
                        }
                    }
                })
                .catch(function (err) {
                    // Don't kill the poller on a single failure
                    console.warn('[CCP] transcript poll error', err);
                });
        };
        // Tick once immediately, then on interval
        tick();
        var intervalId = setInterval(tick, TRANSCRIPT_POLL_INTERVAL_MS);
        
        activePollers[sessionId] = intervalId;
        if (!window._pollerStartTimes) window._pollerStartTimes = {};
        window._pollerStartTimes[sessionId] = Date.now();
        console.log('[CCP] polling /live-transcript every', TRANSCRIPT_POLL_INTERVAL_MS, 'ms for', sessionId);
    }

    function stopTranscriptPolling(sessionId) {
        if (activePollers[sessionId]) {
            clearInterval(activePollers[sessionId]);
            delete activePollers[sessionId];
            console.log('[CCP] stopped polling for', sessionId);
        }
        delete renderedSegmentKeys[sessionId];
    }

    document.addEventListener('DOMContentLoaded', init);
})();
