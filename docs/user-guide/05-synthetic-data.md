# 5. Synthetic Data

**Who:** DataScientist, UseCaseAdmin and PortalAdmin. **Video:** [Module 5: Synthetic Data](https://youtu.be/n-egAmfZiJw).

Defects are rare on a healthy production line, so a classifier often has hundreds of normal images and a handful of defective ones. **Synthetic Data** uses an image-generation model in Amazon Bedrock to paint a described defect onto your real normal images (or to vary your real defect images), lets you approve or reject each result, and appends the approved images to a training manifest so the next training run sees a balanced set.

## 5.1 Sessions

![Synthetic Data Generation sessions](images/05-synthetic-list.png)

The page lists the use case's generation sessions with **Session** (short id, click to open), **Status**, **Object type**, **Defect type**, **Model** and **Created**. Statuses run **Draft** → **Generating** → **Awaiting review** → **Approved** → **Integrated**, with **Failed** for a generation that could not run. Click **Create generation session** to start a new one.

## 5.2 Create a generation session

![New session form](images/05-synthetic-new-session.png)

The form is one page in four panels. The yellow box at the top lists what is still missing; **Create session** enables when it is empty.

**Generation model**

| Field | What to enter |
|-------|---------------|
| **Generation model** | A Bedrock image model available in the Portal's region, listed with its capability flags (inpainting is what defect synthesis needs). The sessions shown used `stability.stable-image-inpaint-v1:0`. If the list is empty, the account has no image-generation model access in this region ("No generation models available"): request model access in the Bedrock console first. |

**Prompt template**

| Field | What to enter |
|-------|---------------|
| **Object type** | The inspected part or product, in plain words: `cookie`, `blue plate`, `metal casting`. |
| **Defect type** | The defect to synthesize: `scratch`, `broken cookie`, `chip`. Required when the source images are normal (there is nothing else to tell the model what to add). |
| **Prompt template** | Entering an object and defect type loads a default template with `{object_type}` and `{defect_type}` placeholders. Edit it to describe lighting, realism and what must stay unchanged, for example "Photorealistic industrial inspection image of a {object_type} exhibiting a {defect_type}… Preserve the original background, camera angle and part appearance." **Save template** keeps your wording for later sessions. |

**Source images**

| Field | What to enter |
|-------|---------------|
| **Dataset** | A dataset prefix discovered in the use case's data bucket, such as the folder of normal training images. |
| **Source classification** | **Defect images** when the chosen images already contain defects (the model produces more variants of them; the defect type may then be left out) or **Normal images** when they are defect-free and a defect must be synthesized (defect type required). |

**Generation controls**

| Field | What to enter |
|-------|---------------|
| **Variations per source image** | 1–20 generated images per source image; default `5`. |
| **Seed** | A randomization seed, or blank for random. Reuse a seed to reproduce a run. |
| **Guidance strength (cfgScale)** | How strongly the prompt steers the model, when the model supports it. |
| **Target manifest key** | The training manifest the approved images will be appended to at integration, for example `datasets/blue-plates/manifests/train.manifest`. |

Click **Create session**. Generation runs in the background; the session shows **Generating** and then **Awaiting review** as previews arrive.

## 5.3 Review, integrate and retrain

Open a session from the list.

![Session detail with previews](images/05-synthetic-session-detail.png)

- The header repeats the **Model**, **Object**, **Defect** and **Status**.
- **Prompt** shows the template used. Edit it and click **Regenerate** to run another pass and compare; each preview is tagged with the pass it came from (**Pass 2**, **Pass 3**, …).
- **Previews** shows every generated image. Click **Approve** or **Reject** under each one, or use **Approve all** / **Reject all**. Only approved images are kept.
- Failed variations are listed under the previews with the error text. In the screenshot a missing Python module on the generation side failed 20 variations; such errors come from the generation backend, not from your inputs.
- **Integrate approved images** copies the approved images into the dataset and appends their manifest lines (labeled with the defect class) to the **Target manifest key**. The banner **Integration complete** reports how many records were appended and to which manifest. Rejected and pending previews are excluded.
- **Start retraining** opens a dialog pre-filled with the updated **Dataset manifest** and asks for a **Model name**; it starts a training job on the enlarged dataset (see [6. Training](06-training.md)). The training job records this session as its origin.

Generated images are staged under `synthetic-staging/` in the use case bucket until they are integrated.
