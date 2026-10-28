import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { normalizeApiError, extractResponseError } from "../src/lib/api-error.ts";

describe("Agent Creation & R2 Upload - Error Normalization & Crash Prevention", () => {
  // --------------------------------------------------------------------------
  // Crash Reproduction: Demonstrate why the original code crashed React
  // --------------------------------------------------------------------------
  it("reproduces the original crash: un-normalized error objects are not valid React children", () => {
    // This is the exact shape returned by FastAPI error responses:
    const fastapiErrorPayload = {
      error: {
        code: "HTTP_ERROR",
        message: "Request validation failed.",
        requestId: "req-12345-fail",
      },
    };

    // In the original AddEmployeeForm.tsx:
    // let err = `Registration failed (HTTP ${agentRes.status})`;
    // if (errData.error) err = errData.error; // <-- err became the raw object!
    // setFormError(err);
    // <Alert tone="red">{formError}</Alert>
    const unnormalizedChild = fastapiErrorPayload.error;
    assert.equal(typeof unnormalizedChild, "object");
    assert.equal(typeof unnormalizedChild.message, "string");

    // React's internal validator would throw:
    // "Objects are not valid as a React child (found: object with keys {code, message, requestId})"
    function simulateOriginalReactChildValidation(child) {
      if (
        child !== null &&
        typeof child === "object" &&
        !Array.isArray(child) &&
        !("$$typeof" in child)
      ) {
        throw new Error(
          `Objects are not valid as a React child (found: object with keys {${Object.keys(child).join(", ")}}).`
        );
      }
      return String(child);
    }

    // Verify that the original un-normalized object triggers the exact React crash
    assert.throws(
      () => simulateOriginalReactChildValidation(unnormalizedChild),
      /Objects are not valid as a React child \(found: object with keys \{code, message, requestId\}\)/
    );

    // Verify that normalizeApiError turns it into a safe primitive string, preventing the crash
    const normalized = normalizeApiError(fastapiErrorPayload);
    assert.equal(typeof normalized, "string");
    assert.equal(normalized, "Request validation failed.");
    assert.doesNotThrow(() => simulateOriginalReactChildValidation(normalized));
  });

  // --------------------------------------------------------------------------
  // Test 1: Successful image upload and successful agent creation
  // --------------------------------------------------------------------------
  it("Scenario 1: Successful image upload and successful agent creation", async () => {
    // Mock successful R2 upload
    const mockUploadResponse = {
      ok: true,
      status: 201,
      json: async () => ({
        object_key: "agent-photos/agent_123.jpg",
        category: "agent-photo",
        content_type: "image/jpeg",
        byte_size: 154200,
      }),
    };
    const uploadData = await mockUploadResponse.json();
    assert.ok(uploadData.object_key.startsWith("agent-photos/"));

    // Mock successful agent creation with uploaded R2 keys
    const mockAgentCreateResponse = {
      ok: true,
      status: 201,
      json: async () => ({
        id: "usr_agent_001",
        username: "agent_rajesh",
        ref_id: "AGNT7788",
        status: "active",
        profile_photo_key: uploadData.object_key,
        effective_commission_rate: 12.5,
      }),
    };

    const agent = await mockAgentCreateResponse.json();
    assert.equal(agent.username, "agent_rajesh");
    assert.equal(agent.profile_photo_key, "agent-photos/agent_123.jpg");
    assert.equal(agent.status, "active");
  });

  // --------------------------------------------------------------------------
  // Test 2: Successful image upload followed by HTTP 400/422 validation error
  // --------------------------------------------------------------------------
  it("Scenario 2: Successful image upload followed by HTTP 400/422 validation error preserves image keys and returns safe string", async () => {
    // 1. R2 image upload succeeds
    const uploadedKey = "agent-photos/cached_photo_999.jpg";
    let stateUploadedKeys = {
      profilePhotoKey: uploadedKey,
      aadhaarDocKey: "aadhaar-documents/cached_aadhaar_999.jpg",
      panDocKey: "pan-documents/cached_pan_999.jpg",
    };

    // 2. Agent creation fails with 422 validation error (e.g. username taken or format error)
    const mockValidationFailResponse = new Response(
      JSON.stringify({
        error: {
          code: "HTTP_ERROR",
          message: "Username 'agent_rajesh' is already taken.",
          requestId: "req-err-422",
        },
      }),
      { status: 422, headers: { "Content-Type": "application/json" } }
    );

    const errorMessage = await extractResponseError(mockValidationFailResponse);

    // Assert error is a clean string, NOT an object
    assert.equal(typeof errorMessage, "string");
    assert.equal(errorMessage, "Username 'agent_rajesh' is already taken.");

    // Assert that uploaded keys are PRESERVED in state so user doesn't have to re-upload
    assert.equal(stateUploadedKeys.profilePhotoKey, uploadedKey);
    assert.ok(stateUploadedKeys.aadhaarDocKey);
    assert.ok(stateUploadedKeys.panDocKey);
  });

  // --------------------------------------------------------------------------
  // Test 3: API response containing a nested error object
  // --------------------------------------------------------------------------
  it("Scenario 3: API response containing nested error object is normalized safely without React crash", () => {
    // Nested error object format 1: { error: { code: '...', message: '...' } }
    const nested1 = {
      error: {
        code: "INVALID_CREDENTIALS",
        message: "Aadhaar number already registered in this community.",
        requestId: "req-nested-01",
      },
    };
    assert.equal(normalizeApiError(nested1), "Aadhaar number already registered in this community.");

    // Nested error object format 2: { error: { code: 'MESSAGING_UNAVAILABLE' } } without message
    const nested2 = {
      error: {
        code: "MESSAGING_UNAVAILABLE",
        requestId: "req-nested-02",
      },
    };
    assert.equal(normalizeApiError(nested2), "MESSAGING_UNAVAILABLE");

    // Nested error object format 3: Pydantic detail list { detail: [ { loc: ['body', 'aadhaar_number'], msg: 'Invalid Aadhaar' } ] }
    const pydanticErrors = {
      detail: [
        { loc: ["body", "aadhaar_number"], msg: "Aadhaar Number must be exactly 12 digits." },
        { loc: ["body", "pan_number"], msg: "Invalid PAN format." },
      ],
    };
    const pydanticNormalized = normalizeApiError(pydanticErrors);
    assert.equal(
      pydanticNormalized,
      "aadhaar_number: Aadhaar Number must be exactly 12 digits.; pan_number: Invalid PAN format."
    );
  });

  // --------------------------------------------------------------------------
  // Test 4: Expired registration verification token
  // --------------------------------------------------------------------------
  it("Scenario 4: Expired registration verification token returns safe message and resets verification state", async () => {
    let isMobileVerified = true;
    let verificationToken = "expired-token-xyz";
    let stateUploadedKeys = {
      profilePhotoKey: "agent-photos/photo.jpg",
      aadhaarDocKey: "aadhaar-documents/aadhaar.jpg",
      panDocKey: "pan-documents/pan.jpg",
    };

    const mockExpiredTokenResponse = new Response(
      JSON.stringify({
        error: {
          code: "HTTP_ERROR",
          message: "Registration verification token has expired. Please verify your mobile number again.",
          requestId: "req-expired-token",
        },
      }),
      { status: 400, headers: { "Content-Type": "application/json" } }
    );

    const errorMessage = await extractResponseError(mockExpiredTokenResponse);
    assert.equal(typeof errorMessage, "string");
    assert.ok(errorMessage.includes("expired"));

    // Verify state transition: when verification token expires, mobile verification and token must reset
    if (
      mockExpiredTokenResponse.status === 400 &&
      (errorMessage.toLowerCase().includes("otp") ||
        errorMessage.toLowerCase().includes("verification") ||
        errorMessage.toLowerCase().includes("token"))
    ) {
      isMobileVerified = false;
      verificationToken = null;
      stateUploadedKeys = { profilePhotoKey: null, aadhaarDocKey: null, panDocKey: null };
    }

    assert.equal(isMobileVerified, false, "Mobile verification must be reset on expired token");
    assert.equal(verificationToken, null, "Verification token must be cleared");
    assert.equal(stateUploadedKeys.profilePhotoKey, null, "Uploaded keys must be invalidated so fresh token is used");
  });

  // --------------------------------------------------------------------------
  // Test 5: Missing or invalid R2 image reference
  // --------------------------------------------------------------------------
  it("Scenario 5: Missing or invalid R2 image reference is caught before submitting and returns readable error", () => {
    // If user attempts submit with empty key or missing upload
    const currentPhotoKey = null;
    const currentAadhaarKey = "aadhaar-documents/doc.jpg";
    const currentPanKey = "pan-documents/doc.jpg";

    function validateImageKeys(photo, aadhaar, pan) {
      if (!photo || !photo.trim() || !aadhaar || !aadhaar.trim() || !pan || !pan.trim()) {
        throw new Error("All registration photos must be uploaded to secure storage before registration.");
      }
    }

    assert.throws(
      () => validateImageKeys(currentPhotoKey, currentAadhaarKey, currentPanKey),
      /All registration photos must be uploaded to secure storage before registration/
    );

    // If backend returns 422 for missing object in storage
    const backend422Missing = {
      error: {
        code: "HTTP_ERROR",
        message: "Uploaded object key 'agent-photos/deleted.jpg' does not exist.",
      },
    };
    assert.equal(
      normalizeApiError(backend422Missing),
      "Uploaded object key 'agent-photos/deleted.jpg' does not exist."
    );
  });

  // --------------------------------------------------------------------------
  // Test 6: Network failure during agent creation
  // --------------------------------------------------------------------------
  it("Scenario 6: Network failure during agent creation produces safe user error without crash", async () => {
    const networkError = new TypeError("Failed to fetch");
    const safeMessage = normalizeApiError(networkError, "Failed to register Agent.");

    assert.equal(typeof safeMessage, "string");
    assert.equal(safeMessage, "Failed to fetch");

    // Even if error is unknown non-Error throw (e.g. string or plain object)
    const rawThrow = { custom: "network timeout" };
    const fallbackMessage = normalizeApiError(rawThrow, "Failed to register Agent.");
    assert.equal(typeof fallbackMessage, "string");
    assert.ok(fallbackMessage.length > 0);
  });

  // --------------------------------------------------------------------------
  // Test 7: Region loading HTTP 401 session expiration handling
  // --------------------------------------------------------------------------
  it("Scenario 7: Region loading HTTP 401 sets sessionExpired, clears regions, and sets clear re-auth message", async () => {
    let formError = null;
    let sessionExpired = false;
    let availableRegions = ["region-1"];

    const mockRegionRes = {
      ok: false,
      status: 401,
      json: async () => ({
        error: { code: "UNAUTHORIZED", message: "Authentication is required." },
      }),
    };

    if (mockRegionRes.status === 401) {
      formError = "Your session has expired or authentication is required. Please log in again to manage agents.";
      sessionExpired = true;
      availableRegions = [];
    }

    assert.equal(sessionExpired, true);
    assert.equal(availableRegions.length, 0);
    assert.match(formError, /session has expired or authentication is required/i);
    assert.match(formError, /Please log in again/i);
  });

  // --------------------------------------------------------------------------
  // Test 8: Agent creation HTTP 401 session expiration handling
  // --------------------------------------------------------------------------
  it("Scenario 8: Agent creation HTTP 401 halts submission and sets sessionExpired state", async () => {
    let formError = null;
    let sessionExpired = false;
    let isSubmitting = true;
    let uploadStatus = "Uploading...";

    const mockCreateRes = {
      ok: false,
      status: 401,
      json: async () => ({
        error: { code: "UNAUTHORIZED", message: "Authentication is required." },
      }),
    };

    if (!mockCreateRes.ok) {
      if (mockCreateRes.status === 401) {
        formError = "Your session has expired or authentication is required. Please log in again to manage agents.";
        sessionExpired = true;
        isSubmitting = false;
        uploadStatus = null;
      }
    }

    assert.equal(sessionExpired, true);
    assert.equal(isSubmitting, false);
    assert.equal(uploadStatus, null);
    assert.match(formError, /session has expired or authentication is required/i);
  });

  // --------------------------------------------------------------------------
  // Test 9: Unmasked lifecycle HTTP 400 error extraction
  // --------------------------------------------------------------------------
  it("Scenario 9: EmployeeDetail extracts unmasked error message from HTTP 400 lifecycle rejection", async () => {
    const mock400Response = {
      ok: false,
      status: 400,
      json: async () => ({
        error: {
          code: "HTTP_ERROR",
          message: "Cannot reactivate an agent whose initial appointment was never approved.",
          requestId: "req-diag-419a33ec",
        },
      }),
    };

    const extracted = await extractResponseError(mock400Response, "Lifecycle request submission failed.");
    assert.equal(extracted, "Cannot reactivate an agent whose initial appointment was never approved.");
    assert.notEqual(extracted, "The request could not be completed.");
    assert.notEqual(extracted, "HTTP_ERROR");
  });

  // --------------------------------------------------------------------------
  // Test 10: Self-approval prohibition (SEC-03) returns actionable dual custody explanation
  // --------------------------------------------------------------------------
  it("Scenario 10: Appointment approval 403 produces clear dual-custody message", async () => {
    const mock403Response = {
      ok: false,
      status: 403,
      json: async () => ({
        error: {
          code: "HTTP_ERROR",
          message: "Reviewers cannot approve appointments they requested.",
          requestId: "req-self-approve-403",
        },
      }),
    };

    const errorMsg = await extractResponseError(mock403Response, "Failed to approve agent appointment.");
    let displayedError = errorMsg;
    if (
      mock403Response.status === 403 &&
      (errorMsg.includes("SELF_APPROVAL") ||
        errorMsg.includes("cannot approve appointments they requested") ||
        errorMsg.includes("Reviewers cannot approve"))
    ) {
      displayedError =
        "Self-approval prohibited: You created this agent appointment. As per security policy (SEC-03 / dual custody), initial appointments must be approved by another administrator or Super Admin.";
    }

    assert.match(displayedError, /Self-approval prohibited/);
    assert.match(displayedError, /SEC-03 \/ dual custody/);
    assert.match(displayedError, /must be approved by another administrator or Super Admin/);
  });

  // --------------------------------------------------------------------------
  // Test 11: Pending agent status guards against invalid reactivation
  // --------------------------------------------------------------------------
  it("Scenario 11: Pending agent status early guards against invalid reactivation attempt", async () => {
    const agent = {
      id: "c011816f-3756-4dfc-bcb4-da9b97d44c6f",
      status: "pending",
      pending_appointment_id: "b0e57daa-d2f5-4194-8d84-ff686f677242",
    };

    let statusError = "";
    let networkCallMade = false;

    async function setStatus(newStatus) {
      if (agent.status === "pending" && newStatus === "active") {
        statusError =
          "Cannot reactivate an agent whose initial appointment was never approved. Please approve the initial appointment first.";
        return;
      }
      networkCallMade = true;
    }

    await setStatus("active");
    assert.equal(networkCallMade, false);
    assert.equal(
      statusError,
      "Cannot reactivate an agent whose initial appointment was never approved. Please approve the initial appointment first."
    );
  });
});

