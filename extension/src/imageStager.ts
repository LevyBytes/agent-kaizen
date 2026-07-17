/** Host-only image validation, bounded paste assembly, and immutable cache staging. */

import * as fs from "node:fs/promises";
import * as path from "node:path";

import { ArtifactStoreError, SecureArtifactStore } from "./artifactStore";
import type { ImageRef } from "./sessionClient";

export const IMAGE_LIMIT = 4;
export const IMAGE_BYTES_LIMIT = 4 * 1024 * 1024;
export const IMAGE_PASTE_CHUNK_LIMIT = 64 * 1024;

export interface StagedImage {
  ref: ImageRef;
  label: string;
}

export interface PasteStart {
  uploadId: string;
  name: string;
  mediaType: string;
  bytes: number;
}

export class ImageStageError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
    this.name = "ImageStageError";
  }
}

/** Validate and content-address host images into the immutable cache. */
export class ImageStager {
  private readonly store: SecureArtifactStore;

  constructor(private readonly workspaceRoot: string) {
    this.store = new SecureArtifactStore(workspaceRoot, "images");
  }

  /** Read a verified absolute plain-file path and stage its bytes. */
  async file(id: string, absolutePath: string): Promise<StagedImage> {
    const content = await this.readSelectedFile(absolutePath);
    return this.bytes(id, content, path.basename(absolutePath));
  }

  /** Validate bounded image bytes and magic, then return a content-addressed image. */
  async bytes(id: string, content: Buffer, name: string, declaredMediaType?: string): Promise<StagedImage> {
    if (content.length === 0 || content.length > IMAGE_BYTES_LIMIT) {
      throw new ImageStageError("DENIED_ATTACHMENT_TOO_LARGE", "Each image must be between 1 byte and 4 MiB");
    }
    const mediaType = detectImageMediaType(content);
    if (!mediaType || (declaredMediaType !== undefined && declaredMediaType !== mediaType)) {
      throw new ImageStageError("DENIED_ATTACHMENT_INVALID", "Image bytes must match PNG, JPEG, WebP, or GIF magic");
    }
    const imageId = validText(id, 128, "DENIED_ATTACHMENT_INVALID");
    const displayName = truncateDerivedName(path.basename(name.trim() || "image"));
    try {
      const staged = await this.store.store(content, { media_type: mediaType, origin: "host_image" });
      return {
        ref: {
          id: imageId,
          kind: "image",
          artifact_ref: staged.artifactRef,
          sha256: staged.sha256,
          bytes: staged.bytes,
          media_type: mediaType,
          name: displayName,
        },
        label: displayName,
      };
    } catch (error) {
      if (error instanceof ArtifactStoreError) throw new ImageStageError(error.code, error.message);
      throw error;
    }
  }

  /** Reject reparse targets and any path identity or size change across the read. */
  private async readSelectedFile(candidate: string): Promise<Buffer> {
    if (!path.isAbsolute(candidate)) throw new ImageStageError("DENIED_ATTACHMENT_PATH", "Selected image path must be absolute");
    const target = path.resolve(candidate);
    let initial;
    try {
      initial = await fs.lstat(target);
      if (initial.isSymbolicLink() || !initial.isFile()) throw new ImageStageError("DENIED_ATTACHMENT_PATH", "Image source is not a plain file");
      const real = await fs.realpath(target);
      if (!samePath(real, target)) throw new ImageStageError("DENIED_ATTACHMENT_PATH", "Image source is a reparse redirect");
    } catch (error) {
      if (error instanceof ImageStageError) throw error;
      throw new ImageStageError("DENIED_ATTACHMENT_PATH", "Image source is unavailable");
    }
    if (initial.size > IMAGE_BYTES_LIMIT) throw new ImageStageError("DENIED_ATTACHMENT_TOO_LARGE", "Each image is limited to 4 MiB");
    const handle = await fs.open(target, "r");
    try {
      const [opened, current, currentReal] = await Promise.all([handle.stat(), fs.lstat(target), fs.realpath(target)]);
      if (current.isSymbolicLink() || !current.isFile() || !samePath(currentReal, target) ||
          opened.dev !== initial.dev || opened.ino !== initial.ino || opened.dev !== current.dev || opened.ino !== current.ino ||
          opened.size !== initial.size || current.size !== initial.size) {
        throw new ImageStageError("DENIED_ATTACHMENT_PATH", "Image source changed during verification");
      }
      const content = await handle.readFile();
      const [after, afterPath, afterReal] = await Promise.all([handle.stat(), fs.lstat(target), fs.realpath(target)]);
      if (afterPath.isSymbolicLink() || !afterPath.isFile() || !samePath(afterReal, target) ||
          after.dev !== opened.dev || after.ino !== opened.ino || after.dev !== afterPath.dev || after.ino !== afterPath.ino ||
          after.size !== opened.size || afterPath.size !== opened.size || content.length !== after.size) {
        throw new ImageStageError("DENIED_ATTACHMENT_PATH", "Image source changed during read");
      }
      return content;
    } finally {
      await handle.close();
    }
  }
}

interface PasteUpload {
  start: PasteStart;
  chunks: Buffer[];
  nextIndex: number;
  received: number;
}

/** Assemble at most four ordered, byte-bounded in-memory paste uploads. */
export class ImageChunkAssembler {
  private readonly uploads = new Map<string, PasteUpload>();

  constructor(private readonly stager: ImageStager) {}

  /** Begin a unique upload with validated declaration metadata. */
  begin(start: PasteStart): void {
    const uploadId = validText(start.uploadId, 128, "DENIED_ATTACHMENT_INVALID");
    if (this.uploads.has(uploadId) || this.uploads.size >= IMAGE_LIMIT || !Number.isSafeInteger(start.bytes) ||
        start.bytes < 1 || start.bytes > IMAGE_BYTES_LIMIT) {
      throw new ImageStageError("DENIED_ATTACHMENT_INVALID", "Image paste declaration is invalid");
    }
    if (!allowedMediaType(start.mediaType)) throw new ImageStageError("DENIED_ATTACHMENT_INVALID", "Image paste media type is unsupported");
    const name = validText(start.name.trim() || "image", 128, "DENIED_ATTACHMENT_INVALID");
    this.uploads.set(uploadId, { start: { ...start, uploadId, name }, chunks: [], nextIndex: 0, received: 0 });
  }

  /** Append the next ordered chunk, cancelling on invalid order or overflow. */
  chunk(uploadId: string, index: number, values: readonly number[]): void {
    const upload = this.uploads.get(uploadId);
    if (!upload || index !== upload.nextIndex || values.length === 0 || values.length > IMAGE_PASTE_CHUNK_LIMIT ||
        values.some((value) => !Number.isInteger(value) || value < 0 || value > 255)) {
      this.cancel(uploadId);
      throw new ImageStageError("DENIED_ATTACHMENT_INVALID", "Image paste chunk is invalid or out of order");
    }
    const chunk = Buffer.from(values);
    upload.received += chunk.length;
    if (upload.received > upload.start.bytes || upload.received > IMAGE_BYTES_LIMIT) {
      this.cancel(uploadId);
      throw new ImageStageError("DENIED_ATTACHMENT_TOO_LARGE", "Image paste exceeded its declared size");
    }
    upload.chunks.push(chunk);
    upload.nextIndex += 1;
  }

  /** Finish only a complete declared upload and stage the joined bytes. */
  async finish(uploadId: string, imageId: string): Promise<StagedImage> {
    const upload = this.uploads.get(uploadId);
    this.uploads.delete(uploadId);
    if (!upload || upload.received !== upload.start.bytes) {
      throw new ImageStageError("DENIED_ATTACHMENT_INVALID", "Image paste ended before all declared bytes arrived");
    }
    return this.stager.bytes(imageId, Buffer.concat(upload.chunks, upload.received), upload.start.name, upload.start.mediaType);
  }

  /** Discard one pending upload. */
  cancel(uploadId: string): void {
    this.uploads.delete(uploadId);
  }

  /** Discard every pending upload. */
  clear(): void {
    this.uploads.clear();
  }
}

/** Enforce four-image, per-image byte, and media-type limits. */
export function validateImageEnvelope(images: readonly StagedImage[]): void {
  if (images.length > IMAGE_LIMIT) throw new ImageStageError("DENIED_ATTACHMENT_COUNT", "At most 4 images are allowed per message");
  for (const image of images) {
    if (image.ref.bytes < 1 || image.ref.bytes > IMAGE_BYTES_LIMIT || !allowedMediaType(image.ref.media_type)) {
      throw new ImageStageError("DENIED_ATTACHMENT_INVALID", "Image attachment metadata is invalid");
    }
  }
}

/** Sniff PNG, JPEG, WebP, or GIF magic bytes. */
export function detectImageMediaType(content: Buffer): ImageRef["media_type"] | undefined {
  if (content.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return "image/png";
  if (content.length >= 3 && content[0] === 0xff && content[1] === 0xd8 && content[2] === 0xff) return "image/jpeg";
  if (content.length >= 12 && content.subarray(0, 4).toString("ascii") === "RIFF" && content.subarray(8, 12).toString("ascii") === "WEBP") return "image/webp";
  if (content.subarray(0, 6).toString("ascii") === "GIF87a" || content.subarray(0, 6).toString("ascii") === "GIF89a") return "image/gif";
  return undefined;
}

function allowedMediaType(value: string): value is ImageRef["media_type"] {
  return value === "image/png" || value === "image/jpeg" || value === "image/webp" || value === "image/gif";
}

/** Trim and bound text while rejecting control and surrogate code points. */
function validText(value: string, max: number, code: string): string {
  const text = typeof value === "string" ? value.trim() : "";
  const points = [...text];
  if (!text || points.length > max || points.some((point) => {
    const codePoint = point.codePointAt(0) ?? 0;
    return codePoint < 32 || (codePoint >= 0x7f && codePoint <= 0x9f) ||
      (codePoint >= 0xd800 && codePoint <= 0xdfff);
  })) throw new ImageStageError(code, "Image metadata is invalid");
  return text;
}

function truncateDerivedName(value: string): string {
  const points = [...value.trim()];
  return validText(points.slice(0, 128).join(""), 128, "DENIED_ATTACHMENT_INVALID");
}

/** Compare resolved paths case-insensitively only on Windows. */
function samePath(left: string, right: string): boolean {
  const a = path.resolve(left);
  const b = path.resolve(right);
  return process.platform === "win32" ? a.toLocaleLowerCase() === b.toLocaleLowerCase() : a === b;
}
